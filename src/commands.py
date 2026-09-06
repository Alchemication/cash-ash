"""Subcommand handlers for the portfolio CLI.

Handlers print user-facing output to stdout and log diagnostics to stderr.
``main.py`` is dispatch only; the work lives here.

Public API:
    cmd_init           -- create and seed a profile's database
    cmd_holdings       -- current positions, cost basis and value
    cmd_concentration  -- grouped weights by security, sector and theme
    cmd_profile        -- create and list profiles
    cmd_context        -- show a profile's context files and their status
    cmd_doctor         -- check whether a profile is ready to use
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from config import (
    CONCENTRATION_ALERT_PCT,
    DEFAULT_MONTHLY_CONTRIBUTION_EUR,
    LARGE_POSITION_WEIGHT_PCT,
    MAX_POSITION_WEIGHT_PCT,
    SEED_RECONCILIATION_TOLERANCE_EUR,
)
from models import Holding
from portfolio import cash_eur, concentration, holdings, total_value
from profiles import (
    CONTEXT_FILES,
    ProfileConfigError,
    add_profile,
    context_status,
    load_profiles,
    resolve_cli_profile,
)
from seed import load_snapshot, reconcile, seed_database
from store import open_db, open_existing_db

logger = logging.getLogger(__name__)


def _eur(value: float | None, *, signed: bool = False) -> str:
    """Format a EUR amount, or a dash when unknown."""
    if value is None:
        return "—"
    return f"{value:+,.2f}" if signed else f"{value:,.2f}"


def _pct(value: float | None, *, signed: bool = False) -> str:
    """Format a percentage, or a dash when unknown."""
    if value is None:
        return "—"
    return f"{value:+.2f}%" if signed else f"{value:.1f}%"


def _pnl_style(value: float | None) -> str:
    """Return a rich style reflecting the sign of a gain or loss."""
    if value is None:
        return "dim"
    return "green" if value >= 0 else "red"


def cmd_init(args: argparse.Namespace) -> None:
    """Create the database and seed it from a broker snapshot file.

    Raises:
        FileNotFoundError: If the snapshot file does not exist.
        ValueError: If the snapshot is malformed, or if the derived total drifts
            from the broker's stated total by more than the tolerance.
    """
    from rich.console import Console
    from rich.table import Table

    console = Console()
    profile, db_path = resolve_cli_profile(
        args.profile, db=args.db, require_existing=False
    )
    if args.snapshot:
        snapshot_path = Path(args.snapshot)
    elif profile is not None:
        snapshot_path = profile.snapshot
    else:
        raise ProfileConfigError(
            "An explicit --db needs an explicit --snapshot; without a profile "
            "there is nowhere to look for one."
        )
    snapshot = load_snapshot(snapshot_path)
    check = reconcile(snapshot)
    difference = check["total_difference_eur"]

    table = Table(title=f"Seed reconciliation — {snapshot_path}", show_lines=False)
    table.add_column("Figure", style="cyan")
    table.add_column("EUR", justify="right")
    table.add_row("Positions", f"{len(snapshot.positions)}")
    table.add_row("Positions value", _eur(check["positions_value_eur"]))
    table.add_row("Cash", _eur(check["cash_eur"]))
    table.add_row("Total", _eur(check["total_eur"]))
    table.add_row(
        f"{snapshot.source.capitalize()} reported total",
        _eur(check["reported_total_eur"]),
    )
    table.add_row(
        "Difference",
        _eur(difference, signed=True),
        style=_pnl_style(
            None
            if difference is None
            else (0 if abs(difference) <= SEED_RECONCILIATION_TOLERANCE_EUR else -1)
        ),
    )
    table.add_row("Derived cost basis", _eur(check["cost_basis_eur"]))
    table.add_row(
        "Implied gain",
        f"{_eur(check['unrealised_gain_eur'], signed=True)} "
        f"({_pct(check['unrealised_return_pct'], signed=True)})",
    )
    console.print(table)

    if difference is None:
        console.print(
            "[yellow]No reported_total_eur in the snapshot, so the "
            "transcription could not be checked.[/yellow]"
        )
    elif abs(difference) > SEED_RECONCILIATION_TOLERANCE_EUR:
        raise ValueError(
            f"Derived total is EUR {difference:+,.2f} away from the reported "
            f"total, over the EUR {SEED_RECONCILIATION_TOLERANCE_EUR:.2f} "
            f"tolerance. A position or a figure is likely mistyped in "
            f"{snapshot_path}. Nothing was written."
        )

    if args.dry_run:
        console.print("[yellow]Dry run: nothing written.[/yellow]")
        return

    conn = open_db(db_path)
    seed_database(conn, snapshot)
    console.print(f"[green]Seeded[/green] {db_path}")
    suffix = f" --profile {profile.name}" if profile and not profile.operator else ""
    console.print(f"Next: [cyan]uv run python main.py holdings{suffix}[/cyan]")


def _holdings_context(
    args: argparse.Namespace,
) -> tuple[list[Holding], float, float, float]:
    """Load holdings, cash, total and the profile's contribution figure.

    Raises:
        ProfileConfigError: If the profile or its database is missing.
    """
    profile, db_path = resolve_cli_profile(args.profile, db=args.db)
    conn = open_existing_db(db_path)
    rows = holdings(conn, account_id=1)
    cash = cash_eur(conn, account_id=1)
    contribution = (
        profile.monthly_contribution_eur
        if profile is not None
        else DEFAULT_MONTHLY_CONTRIBUTION_EUR
    )
    return rows, cash, total_value(rows, cash=cash), contribution


def cmd_holdings(args: argparse.Namespace) -> None:
    """Print current positions with cost basis, value and unrealised P&L."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    rows, cash, total, contribution = _holdings_context(args)

    if not rows:
        console.print(
            "No positions. Run [cyan]uv run python main.py init[/cyan] to seed "
            "the database from your broker snapshot."
        )
        return

    table = Table(title="Holdings", show_lines=False)
    table.add_column("Ticker", style="bold")
    table.add_column("Name", style="dim", overflow="ellipsis", max_width=22)
    table.add_column("Qty", justify="right")
    table.add_column("Cost EUR", justify="right")
    table.add_column("Value EUR", justify="right")
    table.add_column("P&L", justify="right")
    table.add_column("Return", justify="right")
    table.add_column("Weight", justify="right")

    for row in rows:
        pnl = row.unrealised_pnl_eur
        weight = (row.value_eur / total * 100) if row.value_eur and total else None
        weight_text = _pct(weight)
        if weight is not None and weight >= LARGE_POSITION_WEIGHT_PCT:
            weight_text = f"[yellow]{weight_text}[/yellow]"
        table.add_row(
            row.position.security.ticker,
            row.position.security.name,
            f"{row.position.quantity:g}",
            _eur(row.position.cost_basis_eur),
            _eur(row.value_eur),
            f"[{_pnl_style(pnl)}]{_eur(pnl, signed=True)}[/{_pnl_style(pnl)}]",
            f"[{_pnl_style(pnl)}]{_pct(row.unrealised_return_pct, signed=True)}"
            f"[/{_pnl_style(pnl)}]",
            weight_text,
        )

    cost_total = sum(row.position.cost_basis_eur for row in rows)
    positions_value = sum(row.value_eur for row in rows if row.value_eur is not None)
    table.add_section()
    table.add_row(
        "TOTAL",
        "",
        "",
        _eur(cost_total),
        _eur(positions_value),
        f"[{_pnl_style(positions_value - cost_total)}]"
        f"{_eur(positions_value - cost_total, signed=True)}"
        f"[/{_pnl_style(positions_value - cost_total)}]",
        _pct((positions_value - cost_total) / cost_total * 100, signed=True)
        if cost_total
        else "—",
        "",
        style="bold",
    )
    console.print(table)

    unpriced = [row for row in rows if row.value_eur is None]
    priced_at = next((row.price_date for row in rows if row.price_date), None)
    console.print(
        f"Cash [bold]{_eur(cash)}[/bold] · "
        f"Total [bold]{_eur(total)}[/bold] · "
        f"Planning contribution [bold]{_eur(contribution)}[/bold]/month"
    )
    if priced_at:
        console.print(f"[dim]Valued at prices from {priced_at}.[/dim]")
    if unpriced:
        tickers = ", ".join(row.position.security.ticker for row in unpriced)
        console.print(
            f"[yellow]{len(unpriced)} holding(s) could not be priced: {tickers}. "
            f"They are excluded from the total and from all weights.[/yellow]"
        )


def cmd_concentration(args: argparse.Namespace) -> None:
    """Print grouped weights by security, sector and theme."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    rows, cash, total, _ = _holdings_context(args)

    if not rows:
        console.print(
            "No positions. Run [cyan]uv run python main.py init[/cyan] first."
        )
        return

    groupings = ["security", "sector", "theme"] if args.by == "all" else [args.by]
    for key in groupings:
        groups = concentration(rows, key=key, total=total)
        limit = (
            MAX_POSITION_WEIGHT_PCT if key == "security" else CONCENTRATION_ALERT_PCT
        )

        table = Table(title=f"Concentration by {key} (limit {limit:.0f}%)")
        table.add_column(key.capitalize(), style="bold")
        table.add_column("EUR", justify="right")
        table.add_column("Weight", justify="right")
        if key != "security":
            table.add_column("Holdings", style="dim", overflow="fold")

        for group in groups:
            weight = _pct(group.weight_pct)
            if group.over_limit:
                weight = f"[red]{weight}[/red]"
            elif group.weight_pct >= LARGE_POSITION_WEIGHT_PCT:
                weight = f"[yellow]{weight}[/yellow]"
            cells = [group.label, _eur(group.value_eur), weight]
            if key != "security":
                cells.append(", ".join(group.members))
            table.add_row(*cells)

        console.print(table)

        breached = [group for group in groups if group.over_limit]
        if breached:
            for group in breached:
                console.print(
                    f"[red]OVER LIMIT[/red] {group.label} at "
                    f"{group.weight_pct:.1f}% (limit {limit:.0f}%)"
                )
        console.print()

    console.print(
        f"[dim]Weights are against a total of {_eur(total)} including "
        f"{_eur(cash)} cash. Theme weights overlap and may exceed 100%.[/dim]"
    )


def cmd_profile(args: argparse.Namespace) -> None:
    """Create a profile or list the roster.

    Raises:
        ProfileConfigError: If creation is rejected or the roster is invalid.
    """
    from rich.console import Console
    from rich.table import Table

    console = Console()

    if args.profile_cmd == "add":
        profile = add_profile(
            args.name,
            args.telegram_id,
            operator=args.operator,
            monthly_contribution_eur=args.monthly_contribution,
        )
        console.print(
            f"[green]Created profile[/green] {profile.name} at {profile.root}"
        )
        console.print()
        console.print("Next, in order:")
        console.print(
            f"  1. Write your holdings into [cyan]{profile.snapshot}[/cyan] "
            f"(copy [cyan]seed_snapshot.example.toml[/cyan])"
        )
        console.print(
            f"  2. [cyan]uv run python main.py init --profile {profile.name}[/cyan]"
        )
        console.print(
            f"  3. Fill in the context files in [cyan]{profile.context}[/cyan] — "
            f"see [cyan]main.py context[/cyan]"
        )
        return

    profiles = load_profiles()
    table = Table(title="Profiles")
    table.add_column("Name", style="bold")
    table.add_column("Telegram ID", justify="right")
    table.add_column("Default", justify="center")
    table.add_column("Enabled", justify="center")
    table.add_column("Contribution", justify="right")
    table.add_column("Database", style="dim", overflow="fold")
    for profile in sorted(profiles.values(), key=lambda item: item.name):
        table.add_row(
            profile.name,
            str(profile.telegram_id),
            "yes" if profile.operator else "",
            "yes" if profile.enabled else "[red]no[/red]",
            f"{_eur(profile.monthly_contribution_eur)}/mo",
            str(profile.db) if profile.db.exists() else "[yellow]not created[/yellow]",
        )
    console.print(table)


def cmd_context(args: argparse.Namespace) -> None:
    """Show a profile's personal context files and whether they are written.

    Raises:
        ProfileConfigError: If the profile is unknown.
    """
    from rich.console import Console
    from rich.table import Table

    console = Console()
    profile, _ = resolve_cli_profile(args.profile, db=None, require_existing=False)
    assert profile is not None  # resolve_cli_profile only returns None with --db

    table = Table(title=f"Context files — {profile.name}")
    table.add_column("File", style="bold")
    table.add_column("Status")
    table.add_column("Purpose", style="dim", overflow="fold")

    styles = {
        "written": "[green]written[/green]",
        "template": "[yellow]template[/yellow]",
        "missing": "[red]missing[/red]",
    }
    for file, status in context_status(profile):
        label = styles[status]
        if file.generated:
            label += " [dim](generated)[/dim]"
        table.add_row(file.name, label, file.purpose)
    console.print(table)
    console.print(f"[dim]{profile.context}[/dim]")

    unwritten = [
        file.name
        for file, status in context_status(profile)
        if status != "written" and not file.generated
    ]
    if unwritten:
        console.print(
            f"[yellow]Still to write: {', '.join(unwritten)}.[/yellow] Nothing "
            f"reads these yet — the research pipeline will, from Phase 4. "
            f"Writing strategy.md early is the useful one."
        )


def cmd_doctor(args: argparse.Namespace) -> None:
    """Report whether a profile is ready to use, and what is missing."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    table = Table(title="Setup check")
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Detail", style="dim", overflow="fold")

    ok = "[green]ok[/green]"
    warn = "[yellow]todo[/yellow]"
    bad = "[red]missing[/red]"

    try:
        profiles = load_profiles()
    except ProfileConfigError as exc:
        table.add_row("Profile roster", bad, str(exc))
        console.print(table)
        return

    table.add_row("Profile roster", ok, f"{len(profiles)} profile(s)")

    profile, _ = resolve_cli_profile(args.profile, db=None, require_existing=False)
    assert profile is not None
    table.add_row("Profile", ok, f"{profile.name} (telegram {profile.telegram_id})")
    table.add_row(
        "Broker snapshot",
        ok if profile.snapshot.exists() else bad,
        str(profile.snapshot),
    )
    table.add_row(
        "Database",
        ok if profile.db.exists() else bad,
        str(profile.db)
        if profile.db.exists()
        else f"Run 'main.py init --profile {profile.name}'",
    )

    statuses = dict((file.name, status) for file, status in context_status(profile))
    authored = [
        file
        for file in CONTEXT_FILES
        if not file.generated and statuses[file.name] == "written"
    ]
    total_authored = len([file for file in CONTEXT_FILES if not file.generated])
    table.add_row(
        "Context files",
        ok if len(authored) == total_authored else warn,
        f"{len(authored)}/{total_authored} written — see 'main.py context'",
    )

    import os

    table.add_row(
        "Telegram token",
        ok if os.environ.get("TELEGRAM_BOT_TOKEN") else warn,
        "TELEGRAM_BOT_TOKEN in .env — needed from Phase 7",
    )
    provider_keys = [
        name
        for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY")
        if os.environ.get(name)
    ]
    table.add_row(
        "Model providers",
        ok if provider_keys else warn,
        f"{len(provider_keys)} key(s) set — needed from Phase 3",
    )
    console.print(table)
