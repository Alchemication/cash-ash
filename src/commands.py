"""Subcommand handlers for the portfolio CLI.

Handlers print user-facing output to stdout and log diagnostics to stderr.
``main.py`` is dispatch only; the work lives here.

Public API:
    cmd_init           -- create and seed the database
    cmd_holdings       -- current positions, cost basis and value
    cmd_concentration  -- grouped weights by security, sector and theme
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from config import (
    CONCENTRATION_ALERT_PCT,
    LARGE_POSITION_WEIGHT_PCT,
    MAX_POSITION_WEIGHT_PCT,
    MONTHLY_CONTRIBUTION_EUR,
    SEED_RECONCILIATION_TOLERANCE_EUR,
    SEED_SNAPSHOT_PATH,
)
from models import Holding
from portfolio import cash_eur, concentration, holdings, total_value
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
    snapshot_path = Path(args.snapshot or SEED_SNAPSHOT_PATH)
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

    db_path = Path(args.db)
    conn = open_db(db_path)
    seed_database(conn, snapshot)
    console.print(f"[green]Seeded[/green] {db_path}")
    console.print("Next: [cyan]uv run python main.py holdings[/cyan]")


def _holdings_context(args: argparse.Namespace) -> tuple[list[Holding], float, float]:
    """Load holdings, cash and total for a command.

    Raises:
        FileNotFoundError: If the database does not exist yet.
    """
    conn = open_existing_db(Path(args.db))
    rows = holdings(conn, account_id=1)
    cash = cash_eur(conn, account_id=1)
    return rows, cash, total_value(rows, cash=cash)


def cmd_holdings(args: argparse.Namespace) -> None:
    """Print current positions with cost basis, value and unrealised P&L."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    rows, cash, total = _holdings_context(args)

    if not rows:
        console.print(
            "No positions. Run [cyan]uv run python main.py init[/cyan] to seed "
            "the database from the Revolut snapshot."
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
        f"Planning contribution [bold]{_eur(MONTHLY_CONTRIBUTION_EUR)}[/bold]/month"
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
    rows, cash, total = _holdings_context(args)

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
