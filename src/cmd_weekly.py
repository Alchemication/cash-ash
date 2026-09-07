"""Run the whole weekly cycle, and schedule it."""

from __future__ import annotations

import argparse
import logging
import plistlib
import subprocess
import sys
from pathlib import Path

from cmd_daemon import _launchd_environment, _uv_path
from config import (
    LAUNCHD_WEEKLY_LABEL,
    WEEKLY_LOG_FILE,
    WEEKLY_RUN_HOUR,
    WEEKLY_RUN_WEEKDAY,
)
from profiles import resolve_cli_profile
from store import open_existing_db

logger = logging.getLogger(__name__)

_PLIST_DIR = Path.home() / "Library" / "LaunchAgents"

_STAGE_MARK = {True: "[green]ok[/green]", False: "[red]failed[/red]"}


def cmd_weekly(args: argparse.Namespace) -> None:
    """Run sync, triage, research, decide and report in one go.

    Raises:
        ProfileConfigError: If the profile or its database is missing.
        ValueError: If scheduling is requested on an unsupported platform.
    """
    if args.weekly_cmd in {"install", "stop"}:
        _schedule(args)
        return

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
                text=f"<b>{item['action']}</b> {ticker}{amount}\n{item['rationale']}",
                buttons=[
                    [
                        ("Approve", f"rec:{item['id']}:approve"),
                        ("Reject", f"rec:{item['id']}:reject"),
                        ("Later", f"rec:{item['id']}:later"),
                    ]
                ],
            )
        console.print(f"[green]Sent[/green] to {profile.name}.")

    if not outcome.ok:
        console.print(
            "[yellow]Some stages failed. The run continued anyway — a partial "
            "answer beats none when the next attempt is a week away.[/yellow]"
        )


def _schedule(args: argparse.Namespace) -> None:
    """Install or remove the weekly launchd job."""
    from rich.console import Console

    console = Console()
    if sys.platform != "darwin":
        raise ValueError(
            f"launchd is macOS-only and this is {sys.platform}. Schedule "
            f"'main.py weekly --telegram' with cron instead."
        )

    path = _PLIST_DIR / f"{LAUNCHD_WEEKLY_LABEL}.plist"

    if args.weekly_cmd == "stop":
        subprocess.run(
            ["launchctl", "unload", str(path)], check=False, capture_output=True
        )
        console.print(f"[green]Unscheduled[/green] {LAUNCHD_WEEKLY_LABEL}")
        return

    project = Path(__file__).resolve().parent.parent
    log = WEEKLY_LOG_FILE
    log.parent.mkdir(parents=True, exist_ok=True)
    _PLIST_DIR.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": LAUNCHD_WEEKLY_LABEL,
                "ProgramArguments": [
                    _uv_path(),
                    "run",
                    "python",
                    str(project / "main.py"),
                    "weekly",
                    "--telegram",
                ],
                "WorkingDirectory": str(project),
                # launchd runs a missed calendar job once the machine wakes, so
                # a laptop closed on Sunday evening gets its review on Monday
                # rather than skipping the week. That failure has already cost
                # this project an overnight research run.
                "StartCalendarInterval": {
                    "Weekday": WEEKLY_RUN_WEEKDAY,
                    "Hour": WEEKLY_RUN_HOUR,
                    "Minute": 0,
                },
                "RunAtLoad": False,
                "StandardOutPath": str(log),
                "StandardErrorPath": str(log),
                "EnvironmentVariables": _launchd_environment(),
            }
        )
    )
    subprocess.run(["launchctl", "unload", str(path)], check=False, capture_output=True)
    result = subprocess.run(
        ["launchctl", "load", str(path)], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise ValueError(f"launchctl load failed: {result.stderr.strip()}")

    days = [
        "Sunday",
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
    ]
    console.print(
        f"[green]Scheduled[/green] weekly for "
        f"{days[WEEKLY_RUN_WEEKDAY]} at {WEEKLY_RUN_HOUR:02d}:00"
    )
    console.print(f"[dim]{path}[/dim]")
    console.print(f"[dim]Logs: {log}[/dim]")
