"""Render the weekly report, and optionally send it to Telegram."""

from __future__ import annotations

import argparse
import logging
import re

from profiles import resolve_cli_profile
from store import open_existing_db

logger = logging.getLogger(__name__)

_BOT_COMMANDS = [
    ("holdings", "Current positions and value"),
    ("review", "This week's portfolio review"),
    ("pending", "Recommendations awaiting your decision"),
]


def _plain(html: str) -> str:
    """Strip Telegram HTML for terminal display."""
    return re.sub(r"<[^>]+>", "", html)


def cmd_report(args: argparse.Namespace) -> None:
    """Print the weekly report, or send it to Telegram.

    Raises:
        TelegramError: If sending was requested and failed.
        ProfileConfigError: If the profile or its database is missing.
    """
    from rich.console import Console
    from rich.panel import Panel

    from report import weekly_report

    console = Console()
    profile, db_path = resolve_cli_profile(args.profile, db=args.db)
    conn = open_existing_db(db_path)
    parts = weekly_report(conn, profile=profile)

    if not args.telegram:
        console.print(
            Panel(_plain(parts.body), title="Weekly review", border_style="cyan")
        )
        if parts.actionable:
            console.print(
                f"[dim]{len(parts.actionable)} recommendation(s) would carry "
                f"Approve / Reject / Later buttons.[/dim]"
            )
        console.print(
            "[dim]Send it with --telegram once TELEGRAM_BOT_TOKEN is set.[/dim]"
        )
        return

    from notify import send_message, send_with_buttons

    if profile is None:
        raise ValueError("Sending needs a profile; --db alone has no Telegram id.")

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
    console.print(
        f"[green]Sent[/green] to {profile.name} ({len(parts.actionable)} actionable)."
    )


def cmd_telegram_setup(args: argparse.Namespace) -> None:
    """Register the bot's command menu with Telegram.

    Raises:
        TelegramError: If the token is missing or Telegram refused.
    """
    from rich.console import Console

    from notify import set_commands

    console = Console()
    set_commands(_BOT_COMMANDS)
    console.print(
        "[green]Registered[/green] "
        + ", ".join(f"/{name}" for name, _ in _BOT_COMMANDS)
    )
    console.print(
        "[dim]Buttons and chat replies are handled by the daemon: "
        "'main.py daemon install'.[/dim]"
    )
