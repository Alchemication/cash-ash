"""Render the weekly report, and optionally send it to Telegram."""

from __future__ import annotations

import argparse
import logging
import re
from html import unescape

from profiles import resolve_cli_profile
from store import open_existing_db

logger = logging.getLogger(__name__)

_BOT_COMMANDS = [
    ("holdings", "Current positions and value"),
    ("review", "This week's portfolio review"),
    ("pending", "This week's recommendations"),
    ("record", "What following past recommendations would have done"),
    ("refresh", "Download and reconcile the latest broker statement"),
    ("thesis", "Compare owner thesis and proposed changes"),
    ("evidence", "Dated findings and source excerpts"),
    ("reset", "Forget the current conversation"),
]
"""The menu Telegram offers. Deliberately short.

Only the commands worth a menu entry are listed. Everything else is asked in
plain words — a menu of twenty commands is the thing the chat agent exists to
replace.
"""


def _plain(html: str) -> str:
    """Strip Telegram HTML for terminal display."""
    return unescape(re.sub(r"<[^>]+>", "", html))


def cmd_report(args: argparse.Namespace) -> None:
    """Print the weekly review, or send it to Telegram.

    Printing is the dry run: the same summary and cards, nothing sent and no
    model called.

    Raises:
        TelegramError: If sending was requested and failed.
        ProfileConfigError: If the profile or its database is missing.
    """
    from rich.console import Console
    from rich.panel import Panel

    from report import card_text, weekly_report

    console = Console()
    profile, db_path = resolve_cli_profile(args.profile, db=args.db)
    conn = open_existing_db(db_path)

    if not args.telegram:
        parts = weekly_report(conn, profile=profile)
        console.print(
            Panel(_plain(parts.body), title="Weekly review", border_style="cyan")
        )
        for number, item in enumerate(parts.actionable, 1):
            console.print(
                Panel(
                    _plain(card_text(item)),
                    title=f"Card {number} of {len(parts.actionable)}",
                    border_style="yellow",
                )
            )
        console.print(
            "[dim]Dry run: nothing sent. Each card carries Done or Approve trade, "
            "Reject, Snooze, Evidence and Thesis buttons. Send with --telegram "
            "once TELEGRAM_BOT_TOKEN is set.[/dim]"
        )
        return

    from report_delivery import send_review

    if profile is None:
        raise ValueError("Sending needs a profile; --db alone has no Telegram id.")

    parts = send_review(conn, profile=profile)
    console.print(
        f"[green]Sent[/green] to {profile.name} ({len(parts.actionable)} card(s))."
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
