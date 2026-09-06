"""Rank every holding by what deserves a full research pass this week."""

from __future__ import annotations

import argparse
import logging

from profiles import resolve_cli_profile
from store import open_existing_db

logger = logging.getLogger(__name__)


def cmd_triage(args: argparse.Namespace) -> None:
    """Run triage across the portfolio and show the ranking.

    Raises:
        ValueError: If there is nothing to triage or the output is unusable.
        ProfileConfigError: If the profile or its database is missing.
        LLMError: If the model call fails outright.
    """
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    from research import run_triage, triage_inputs

    console = Console()
    _, db_path = resolve_cli_profile(args.profile, db=args.db)
    conn = open_existing_db(db_path)

    if args.dry_run:
        entries = triage_inputs(conn)
        console.print(
            Panel(
                "\n\n".join(entry.render() for entry in entries),
                title=f"Triage input — {len(entries)} holdings (nothing sent)",
                border_style="dim",
            )
        )
        return

    console.print("Ranking every holding by what may have changed…\n")
    run_id, rankings, note = run_triage(conn)

    table = Table(title=f"Triage — run {run_id}")
    table.add_column("#", justify="right")
    table.add_column("Ticker", style="bold")
    table.add_column("Depth", justify="center")
    table.add_column("Signals", style="dim")
    table.add_column("Reason", overflow="fold")

    for item in rankings:
        table.add_row(
            str(item["rank"]),
            item["ticker"],
            "[green]yes[/green]" if item["selected"] else "",
            ", ".join(item["signals"]),
            item["reason"],
        )
    console.print(table)

    selected = [item for item in rankings if item["selected"]]
    if note:
        console.print(Panel(note, title="portfolio", border_style="cyan"))
    if selected:
        names = ", ".join(item["ticker"] for item in selected)
        console.print(
            f"[bold]{len(selected)} of {len(rankings)}[/bold] selected for depth: {names}"
        )
    else:
        console.print(
            "[green]Nothing needs a deeper look this week.[/green] That is a "
            "normal outcome, not a failure."
        )
