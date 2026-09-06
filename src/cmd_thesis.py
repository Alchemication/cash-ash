"""Bootstrap, inspect and compare investment theses."""

from __future__ import annotations

import argparse
import logging

from profiles import resolve_cli_profile
from store import load_securities, open_existing_db
from store_research import active_thesis, thesis_history

logger = logging.getLogger(__name__)

_CONVICTION_STYLE = {
    "strong": "green",
    "moderate": "cyan",
    "weak": "yellow",
    "none": "red",
    "unstated": "dim",
}

_STATUS_STYLE = {
    "improving": "green",
    "unchanged": "cyan",
    "deteriorating": "yellow",
    "broken": "red",
    "unexamined": "dim",
}


def _resolve_security(conn, ticker: str):  # type: ignore[no-untyped-def]
    """Return the security for *ticker*, or raise with the known list."""
    securities = load_securities(conn)
    security = securities.get(ticker.upper())
    if security is None:
        known = ", ".join(sorted(securities))
        raise ValueError(f"Unknown ticker {ticker!r}. Known: {known}.")
    return security


def cmd_thesis(args: argparse.Namespace) -> None:
    """Show, list or bootstrap theses.

    Raises:
        ValueError: If a ticker is unknown or no notes exist to build from.
        ProfileConfigError: If the profile or its database is missing.
    """
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console()
    profile, db_path = resolve_cli_profile(args.profile, db=args.db)
    conn = open_existing_db(db_path)

    if args.thesis_cmd == "bootstrap":
        from research import bootstrap_theses

        if profile is None:
            raise ValueError("Bootstrapping needs a profile; --db alone has no notes.")
        console.print(
            "Restating your own reasons as structured theses. This does not "
            "research anything — it only turns what you wrote into something "
            "trackable.\n"
        )
        results = bootstrap_theses(
            conn,
            profile=profile,
            only=args.ticker,
            overwrite=args.overwrite,
        )

        table = Table(title="Bootstrapped theses")
        table.add_column("Ticker", style="bold")
        table.add_column("Conviction")
        table.add_column("Breaks if", justify="right")
        table.add_column("Summary", overflow="fold")
        for ticker, thesis, skipped in results:
            if thesis is None:
                table.add_row(ticker, "[red]failed[/red]", "—", skipped or "")
                continue
            style = _CONVICTION_STYLE.get(thesis.conviction, "dim")
            note = "[yellow]kept[/yellow]" if skipped else ""
            table.add_row(
                ticker,
                f"[{style}]{thesis.conviction}[/{style}]",
                str(len(thesis.what_would_break_it)),
                f"{note} {thesis.summary}".strip(),
            )
        console.print(table)

        weak = [
            t for _, t, s in results if t and not s and t.conviction in {"none", "weak"}
        ]
        if weak:
            console.print(
                f"[yellow]{len(weak)} holding(s) rest on a reason not connected "
                f"to the business or the price.[/yellow] That is a finding, not "
                f"a failure — it is what REVIEW exists for."
            )
        console.print("Inspect one with [cyan]main.py thesis show TICKER[/cyan].")
        return

    if args.thesis_cmd == "show":
        security = _resolve_security(conn, args.ticker)
        assert security.id is not None
        versions = thesis_history(conn, security_id=security.id)
        if not versions:
            console.print(
                f"No thesis for {security.ticker}. Run "
                f"[cyan]main.py thesis bootstrap[/cyan] to create one from your notes."
            )
            return

        current = versions[0]
        console.print(
            Panel(
                current.summary,
                title=f"{security.ticker} — {security.name} (v{current.version})",
                border_style="bold",
            )
        )
        meta = Table.grid(padding=(0, 2))
        meta.add_column(style="cyan")
        meta.add_column()
        conviction_style = _CONVICTION_STYLE.get(current.conviction, "dim")
        status_style = _STATUS_STYLE.get(current.thesis_status, "dim")
        meta.add_row(
            "conviction",
            f"[{conviction_style}]{current.conviction}[/{conviction_style}]",
        )
        meta.add_row(
            "status", f"[{status_style}]{current.thesis_status}[/{status_style}]"
        )
        meta.add_row("source", current.source)
        meta.add_row("versions", str(len(versions)))
        console.print(meta)

        if current.rationale:
            console.print(
                Panel(current.rationale, title="rationale", border_style="blue")
            )
        for title, items, style in (
            ("assumptions — must stay true", current.key_assumptions, "cyan"),
            ("open questions", current.open_questions, "yellow"),
            ("what would break it", current.what_would_break_it, "magenta"),
        ):
            if items:
                console.print(
                    Panel(
                        "\n".join(f"• {item}" for item in items),
                        title=title,
                        border_style=style,
                    )
                )
        if not current.is_falsifiable:
            console.print(
                "[red]This thesis states nothing that would prove it wrong.[/red] "
                "Nothing can detect that it stopped being true."
            )
        return

    securities = {security.id: security for security in load_securities(conn).values()}
    table = Table(title="Theses")
    table.add_column("Ticker", style="bold")
    table.add_column("v", justify="right")
    table.add_column("Conviction")
    table.add_column("Status")
    table.add_column("Breaks if", justify="right")
    table.add_column("Summary", overflow="fold")

    rows = 0
    for security in sorted(securities.values(), key=lambda s: s.ticker):
        assert security.id is not None
        thesis = active_thesis(conn, security_id=security.id)
        if thesis is None:
            continue
        rows += 1
        conviction_style = _CONVICTION_STYLE.get(thesis.conviction, "dim")
        status_style = _STATUS_STYLE.get(thesis.thesis_status, "dim")
        table.add_row(
            security.ticker,
            str(thesis.version),
            f"[{conviction_style}]{thesis.conviction}[/{conviction_style}]",
            f"[{status_style}]{thesis.thesis_status}[/{status_style}]",
            str(len(thesis.what_would_break_it))
            if thesis.is_falsifiable
            else "[red]0[/red]",
            thesis.summary,
        )
    if not rows:
        console.print(
            "No theses yet. Run [cyan]main.py thesis bootstrap[/cyan] to create "
            "them from your own notes."
        )
        return
    console.print(table)
