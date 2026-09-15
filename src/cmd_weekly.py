"""Run the whole weekly cycle by hand.

Scheduling lives in the daemon, which checks every half hour whether the week's
run has happened. This command is for running it now — to see what it does, or
after fixing something the scheduled run tripped over.
"""

from __future__ import annotations

import argparse
import logging
import re
from datetime import date
from html import unescape

from profiles import resolve_cli_profile
from store import open_existing_db

logger = logging.getLogger(__name__)

_STAGE_MARK = {True: "[green]ok[/green]", False: "[red]failed[/red]"}


def cmd_weekly(args: argparse.Namespace) -> None:
    """Run sync, triage, research, decide and report in one go.

    With ``--telegram`` the run is followed live in one message, which gives
    way to the summary and a card per decision when it finishes.

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
    if args.telegram and profile is None:
        # Checked before the run: learning after the research that there was
        # nowhere to send it wastes the run.
        raise ValueError("Sending needs a profile; --db alone has no Telegram id.")
    conn = open_existing_db(db_path)

    progress = None
    if args.telegram:
        from report_delivery import LiveProgress

        progress = LiveProgress(chat_id=profile.telegram_id, run_date=date.today())

    console.print("[dim]Running the weekly cycle. This takes a few minutes.[/dim]\n")
    outcome = run_weekly(
        conn,
        profile=profile,
        max_research=args.max_research,
        skip_research=args.skip_research,
        progress=progress,
    )

    table = Table(title="Weekly run")
    table.add_column("Stage", style="bold")
    table.add_column("Result")
    table.add_column("Time", justify="right")
    table.add_column("Detail", overflow="fold")
    for stage in outcome.stages:
        mark = "[dim]skipped[/dim]" if stage.skipped else _STAGE_MARK[stage.ok]
        seconds = f"{stage.seconds:.0f}s" if stage.seconds is not None else ""
        table.add_row(stage.name, mark, seconds, stage.detail)
    console.print(table)

    if outcome.report:
        console.print(
            Panel(
                unescape(re.sub(r"<[^>]+>", "", outcome.report)),
                title="Report",
                border_style="cyan",
            )
        )

    if args.telegram and outcome.report:
        from report_delivery import send_review

        parts = send_review(
            conn,
            profile=profile,
            progress_message_id=progress.message_id if progress else None,
        )
        console.print(
            f"[green]Sent[/green] to {profile.name} ({len(parts.actionable)} card(s))."
        )

    if not outcome.ok:
        console.print(
            "[yellow]Some stages failed. The run continued anyway — a partial "
            "answer beats none when the next attempt is a week away.[/yellow]"
        )
