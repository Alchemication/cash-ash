"""Run a deep research pass on one holding, or on whatever triage selected."""

from __future__ import annotations

import argparse
import logging

from profiles import resolve_cli_profile
from store import open_existing_db

logger = logging.getLogger(__name__)

_STATUS_STYLE = {
    "improving": "green",
    "unchanged": "cyan",
    "deteriorating": "yellow",
    "broken": "red",
}


def _latest_triage_selection(conn) -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    """Return (ticker, reason) for holdings the most recent triage selected."""
    row = conn.execute(
        "SELECT id FROM research_run WHERE kind = 'triage' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return []
    return [
        (item["ticker"], item["reason"])
        for item in conn.execute(
            """
            SELECT s.ticker, t.reason
            FROM triage_result t
            JOIN securities s ON s.id = t.security_id
            WHERE t.run_id = ? AND t.selected = 1
            ORDER BY t.rank
            """,
            (row["id"],),
        )
    ]


def cmd_research(args: argparse.Namespace) -> None:
    """Research one holding, or every holding the last triage selected.

    Raises:
        ValueError: If a ticker or its thesis is missing, or output is unusable.
        ProfileConfigError: If the profile or its database is missing.
    """
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    from research import research_security

    console = Console()
    _, db_path = resolve_cli_profile(args.profile, db=args.db)
    conn = open_existing_db(db_path)

    if args.ticker:
        targets = [(args.ticker.upper(), "requested directly")]
    else:
        targets = _latest_triage_selection(conn)
        if not targets:
            console.print(
                "Nothing selected by the last triage, and no ticker given. Run "
                "[cyan]main.py triage[/cyan] first, or name a holding."
            )
            return
        console.print(
            f"Researching {len(targets)} holding(s) selected by the last "
            f"triage: {', '.join(t for t, _ in targets)}\n"
        )

    for ticker, trigger in targets:
        console.print(f"[bold]{ticker}[/bold] — {trigger}")
        try:
            result = research_security(conn, ticker=ticker, trigger=trigger)
        except ValueError as exc:
            console.print(f"  [red]{exc}[/red]\n")
            continue

        style = _STATUS_STYLE.get(result.thesis_status, "dim")
        console.print(
            f"  thesis [{style}]{result.thesis_status}[/{style}] — "
            f"{result.status_reason}"
        )

        table = Table(show_header=True, header_style="dim", box=None, padding=(0, 1))
        table.add_column("Q", overflow="fold")
        table.add_column("Finding", overflow="fold")
        table.add_column("Basis")
        for answer in result.answers:
            kind = str(answer.get("kind", "background"))
            colour = {"sourced": "green", "unanswered": "dim"}.get(kind, "yellow")
            table.add_row(
                str(answer.get("question", ""))[:70],
                str(answer.get("answer", ""))[:160],
                f"[{colour}]{kind}[/{colour}]",
            )
        console.print(table)

        if result.triggered:
            console.print(
                Panel(
                    "\n".join(f"• {item}" for item in result.triggered),
                    title="stated breaking conditions that have occurred",
                    border_style="red",
                )
            )
        console.print(
            f"  [dim]{result.evidence_count} claim(s) stored, "
            f"{result.sourced_count} with a source · run {result.run_id}[/dim]"
        )
        if result.proposed_version is not None:
            console.print(
                f"  [yellow]Proposed thesis v{result.proposed_version}.[/yellow] "
                f"Nothing changed yet — review with "
                f"[cyan]main.py thesis show {ticker}[/cyan]."
            )
        console.print()
