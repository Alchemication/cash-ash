"""Profile-scoped Revolut statement ingestion and attended browser download CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

from profiles import Profile, resolve_cli_profile
from revolut_reconcile import apply_seed_corrections, reconcile, seed_corrections
from revolut_statement import extract_pdf
from revolut_storage import ingest
from store import open_existing_db

logger = logging.getLogger(__name__)


def cmd_revolut(args: argparse.Namespace) -> None:
    """Archive source evidence and print a current-book quantity comparison."""
    if args.revolut_cmd == "settings":
        from config import (
            REVOLUT_BROWSER_TIMEOUT_MS,
            REVOLUT_CALENDAR_MAX_STEPS,
            REVOLUT_DOWNLOAD_TIMEOUT_MS,
            REVOLUT_MONEY_TOLERANCE,
            REVOLUT_QUANTITY_TOLERANCE,
            REVOLUT_SEED_QUANTITY_DECIMALS,
            REVOLUT_SIGN_IN_POLL_S,
            REVOLUT_SIGN_IN_TIMEOUT_S,
        )

        print(
            json.dumps(
                {
                    "REVOLUT_BROWSER_TIMEOUT_MS": REVOLUT_BROWSER_TIMEOUT_MS,
                    "REVOLUT_CALENDAR_MAX_STEPS": REVOLUT_CALENDAR_MAX_STEPS,
                    "REVOLUT_DOWNLOAD_TIMEOUT_MS": REVOLUT_DOWNLOAD_TIMEOUT_MS,
                    "REVOLUT_MONEY_TOLERANCE": REVOLUT_MONEY_TOLERANCE,
                    "REVOLUT_QUANTITY_TOLERANCE": REVOLUT_QUANTITY_TOLERANCE,
                    "REVOLUT_SEED_QUANTITY_DECIMALS": REVOLUT_SEED_QUANTITY_DECIMALS,
                    "REVOLUT_SIGN_IN_POLL_S": REVOLUT_SIGN_IN_POLL_S,
                    "REVOLUT_SIGN_IN_TIMEOUT_S": REVOLUT_SIGN_IN_TIMEOUT_S,
                },
                indent=2,
            )
        )
        return
    profile, db_path = resolve_cli_profile(args.profile)
    assert profile is not None
    archive = None
    # Refuse an invalid/missing database before opening Chrome or archiving data.
    with closing(open_existing_db(db_path)) as conn:
        if args.revolut_cmd == "correct-seed":
            _correct_seed(conn, profile, Path(args.pdf), assume_yes=args.yes)
            return
        if args.revolut_cmd == "refresh":
            _offer_refresh(profile)
            return
        if args.revolut_cmd == "download":
            from revolut_download import download_statement

            statement, archive = asyncio.run(
                download_statement(
                    profile,
                    reuse_session=Path(args.reuse_session)
                    if args.reuse_session
                    else None,
                    start=date.fromisoformat(args.start) if args.start else None,
                    end=date.fromisoformat(args.end) if args.end else None,
                    manual_navigation=args.manual_navigation,
                )
            )
        elif args.revolut_cmd == "ingest":
            statement, archive = ingest(profile, Path(args.pdf))
        else:
            statement = extract_pdf(Path(args.pdf).expanduser())
        report = {
            "archive": str(archive) if archive else None,
            "statement": statement.to_dict(),
            "reconciliation": reconcile(conn, statement),
        }
    print(json.dumps(report, indent=2))


def _offer_refresh(profile: Profile) -> None:
    """Send the owner the Run now broker-refresh proposal.

    The button is handled by the running daemon, which drives Chrome and relays
    the sign-in link. Without a bot token or a running daemon nothing happens
    when it is tapped, so both are checked or noted here.
    """
    from config import TELEGRAM_BOT_TOKEN
    from daemon_refresh import offer_broker_refresh

    if not TELEGRAM_BOT_TOKEN:
        raise ValueError(
            "No TELEGRAM_BOT_TOKEN in .env, so the refresh proposal cannot be "
            "sent. The refresh runs through Telegram; set a token and start the "
            "daemon first."
        )
    offer_broker_refresh(profile)
    logger.info(
        "Sent the broker-refresh proposal to %s. The running daemon handles "
        "'Run now'; start it with 'main.py daemon' if it is not.",
        profile.name,
    )


def _correct_seed(
    conn: sqlite3.Connection, profile: Profile, pdf: Path, *, assume_yes: bool
) -> None:
    """Show the truncated seed quantities a statement corrects; apply on consent."""
    from rich.console import Console
    from rich.table import Table

    statement, archive = ingest(profile, pdf)
    corrections, refusals = seed_corrections(conn, statement)
    console = Console()
    for refusal in refusals:
        console.print(
            f"Skipped {refusal['symbol']} ({refusal['currency']}): "
            f"{refusal['reason']}.",
            markup=False,
        )
    if not corrections:
        console.print("No truncated seed quantities to correct.")
        return

    table = Table(title=f"Seed quantity corrections from statement {archive.name[:12]}")
    table.add_column("Symbol")
    table.add_column("Quantity")
    table.add_column("Value added (native)", justify="right")
    for correction in corrections:
        table.add_row(
            correction.ticker,
            f"{correction.before:g} → {correction.after.normalize()}",
            f"{correction.value_added:,.2f} {correction.currency}",
        )
    console.print(table)
    console.print(
        "Cost basis and cash stay unchanged: the seed's EUR value already covered "
        "the full holding."
    )
    if not assume_yes:
        try:
            answer = input(f"Apply {len(corrections)} corrections? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in {"y", "yes"}:
            console.print("Nothing changed.")
            return

    apply_seed_corrections(conn, corrections, archive.name)
    # Read the ledger back rather than trusting the plan: derived is the answer.
    remaining = sum(
        row["status"] != "quantity_match" for row in reconcile(conn, statement)["rows"]
    )
    console.print(
        f"Corrected {len(corrections)} synthetic opening trades; {remaining} "
        f"statement rows still differ from the ledger."
    )
