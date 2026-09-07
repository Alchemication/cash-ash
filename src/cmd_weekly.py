"""Run the whole weekly cycle by hand.

Scheduling lives in the daemon, which checks every half hour whether the week's
run has happened. This command is for running it now — to see what it does, or
after fixing something the scheduled run tripped over.
"""

from __future__ import annotations

import argparse
import logging
from review_text import recommendation_buttons

from notify import escape
from profiles import resolve_cli_profile
from store import open_existing_db

logger = logging.getLogger(__name__)

_STAGE_MARK = {True: "[green]ok[/green]", False: "[red]failed[/red]"}


def cmd_weekly(args: argparse.Namespace) -> None:
    """Run sync, triage, research, decide and report in one go.

    Raises:
        ProfileConfigError: If the profile or its database is missing.
        ValueError: If sending is requested without a profile.
    """
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    from weekly import run_weekly

    console = Console()
    profile, db_path = resolve_cli_profile(args.profile, db=args.db)
    conn = open_existing_db(db_path)

    console.print("[dim]Running the weekly cycle. This takes a few minutes.[/dim]\n")
    outcome = run_weekly(
        conn,
        profile=profile,
        max_research=args.max_research,
        skip_research=args.skip_research,
    )

    table = Table(title="Weekly run")
    table.add_column("Stage", style="bold")
    table.add_column("Result")
    table.add_column("Detail", overflow="fold")
    for stage in outcome.stages:
        mark = "[dim]skipped[/dim]" if stage.skipped else _STAGE_MARK[stage.ok]
        table.add_row(stage.name, mark, stage.detail)
    console.print(table)

    if outcome.report:
        console.print(
            Panel(
                __import__("re").sub(r"<[^>]+>", "", outcome.report),
                title="Report",
                border_style="cyan",
            )
        )

    if args.telegram and outcome.report:
        if profile is None:
            raise ValueError("Sending needs a profile; --db alone has no Telegram id.")
        from notify import send_message, send_with_buttons
        from report import weekly_report

        parts = weekly_report(conn, profile=profile)
        send_message(chat_id=profile.telegram_id, text=parts.body)
        for item in parts.actionable:
            ticker = f"{item['ticker']} " if item["ticker"] else ""
            amount = f" — €{item['amount_eur']:,.2f}" if item["amount_eur"] else ""
            send_with_buttons(
                chat_id=profile.telegram_id,
                text=f"<b>{item['action']}</b> {ticker}{amount}\n{escape(item['rationale'])}",
                buttons=recommendation_buttons(item),
            )
        console.print(f"[green]Sent[/green] to {profile.name}.")

    if not outcome.ok:
        console.print(
            "[yellow]Some stages failed. The run continued anyway — a partial "
            "answer beats none when the next attempt is a week away.[/yellow]"
        )
