"""skarbie — research and decision support for a small personal stock portfolio.

Subcommands:
    profile        Create and list profiles — one per person.
    init           Create the database and seed it from a broker snapshot file.
    sync           Fetch prices, FX rates, known dates and consensus estimates.
    events         List known upcoming events for your holdings.
    thesis         Bootstrap, list and inspect why each position is held.
    triage         Rank every holding by what deserves attention this week.
    research       Deep pass on a holding: plan, evidence, findings.
    recommend      Propose actions for the week, with the rules applied.
    decide         Record approve / reject / later on a recommendation.
    weekly         The whole cycle: sync, triage, research, decide, report.
    report         The weekly review, on screen or sent to Telegram.
    telegram-setup Register the bot's command menu.
    daemon         Listen for button presses; install it under launchd.
    models         Inspect or change which model each stage calls.
    llm-log        Inspect recorded model calls and what they cost.
    price          Record one price by hand when a feed cannot.
    holdings       Show current positions, cost basis, value and P&L.
    concentration  Show grouped weights by security, sector and theme.
    context        Show a profile's personal context files and their status.
    doctor         Check whether a profile is ready to use.
    db             Migration and schema admin.

All portfolio commands are profile-scoped. Use --profile NAME; omitting it
means the operator profile from profiles.toml. An explicit --db is only for
experimental databases and never creates one.

Examples:
    uv run python main.py profile add adam --telegram-id 123456789 --operator
        Create the first profile, its directories and its context templates.

    uv run python main.py init --dry-run
        Check the snapshot reconciles against the broker's stated total,
        writing nothing.

    uv run python main.py init
        Create and seed the database.

    uv run python main.py holdings
        Current positions valued at the latest known prices.

    uv run python main.py concentration --by theme
        Theme weights, flagging anything over the configured limit.

    uv run python main.py sync
        Fetch today's prices and FX rates for everything you hold.

    uv run python main.py price SPCX --close 147.95
        Record a price by hand when the feed cannot.

    uv run python main.py events --days 60
        Earnings dates, dividends and curated events in the next 60 days.

    uv run python main.py thesis bootstrap
        Turn your written reasons for each holding into trackable theses.

    uv run python main.py thesis show NKE
        Why you own it, what must stay true, and what would break it.

    uv run python main.py triage
        Rank every holding by what may have changed this week.

    uv run python main.py triage --dry-run
        Show exactly what triage would be given, without calling a model.

    uv run python main.py research AMD
        Plan questions, gather evidence, and judge whether the thesis holds.

    uv run python main.py research
        The same, for everything the last triage selected.

    uv run python main.py recommend
        Propose this week's actions, with the position and sell rules applied.

    uv run python main.py decide
        List recommendations and what you decided about each.

    uv run python main.py decide 3 approve --note "agreed, will buy Monday"
        Record a decision. Approving is not executing.

    uv run python main.py weekly
        Sync, triage, research, decide and report, in one go.

    uv run python main.py weekly install
        Schedule it. Sunday evening, so there is a day to think before Monday.

    uv run python main.py report
        The weekly review as it would read on a phone.

    uv run python main.py report --telegram
        Send it, with Approve / Reject / Later buttons on anything actionable.

    uv run python main.py daemon
        Listen for button presses in the foreground.

    uv run python main.py daemon install
        Run it under launchd, so it survives logout and sleep.

    uv run python main.py models
        Which model each stage calls, and what tier it is.

    uv run python main.py models cost
        What the models have actually cost so far.

    uv run python main.py llm-log --id 42
        The full trace of one call: prompt, reasoning, response, cost.

    uv run python main.py holdings --profile kasia
        Someone else's portfolio.

    uv run python main.py doctor
        What is set up and what is still missing.

    uv run python main.py db status
        Row counts and migration state.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Ensure src/ is on the path when running from the project root
sys.path.insert(0, str(Path(__file__).parent / "src"))

from cmd_daemon import cmd_daemon  # noqa: E402
from cmd_db import cmd_db  # noqa: E402
from cmd_llm_log import cmd_llm_log  # noqa: E402
from cmd_models import cmd_models  # noqa: E402
from cmd_thesis import cmd_thesis  # noqa: E402
from cmd_weekly import cmd_weekly  # noqa: E402
from cmd_recommend import cmd_decide, cmd_recommend  # noqa: E402
from cmd_report import cmd_report, cmd_telegram_setup  # noqa: E402
from cmd_research import cmd_research  # noqa: E402
from cmd_triage import cmd_triage  # noqa: E402
from cmd_sync import cmd_events, cmd_price, cmd_sync  # noqa: E402
from commands import (  # noqa: E402
    cmd_concentration,
    cmd_context,
    cmd_doctor,
    cmd_holdings,
    cmd_init,
    cmd_profile,
)
from config import DEFAULT_MONTHLY_CONTRIBUTION_EUR  # noqa: E402
from log import setup_logging  # noqa: E402
from llm import LLMError  # noqa: E402
from market_data import ProviderError  # noqa: E402
from notify import TelegramError  # noqa: E402
from model_prefs import FEATURES  # noqa: E402
from profiles import ProfileConfigError  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    """Build the full argument parser.

    Separate from :func:`main` so tests can walk the command tree instead of
    scraping this file for string literals — subcommands built in a loop have
    no literal to find, which is exactly how a documentation check goes
    vacuously green.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        description="Personal portfolio research and decision support",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Enable debug logging on stderr"
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    def _add_db(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--profile",
            metavar="NAME",
            default=None,
            help="Profile name (default: the operator profile)",
        )
        subparser.add_argument(
            "--db",
            metavar="PATH",
            default=None,
            help="Explicit database path, overriding --profile",
        )

    p_profile = sub.add_parser("profile", help="Create and list profiles")
    profile_sub = p_profile.add_subparsers(dest="profile_cmd", required=True)
    p_profile_add = profile_sub.add_parser("add", help="Create a profile")
    p_profile_add.add_argument("name", help="Profile name (lowercase, no spaces)")
    p_profile_add.add_argument(
        "--telegram-id",
        type=int,
        required=True,
        metavar="ID",
        help="Numeric Telegram user id; message @userinfobot to find it",
    )
    p_profile_add.add_argument(
        "--operator",
        action="store_true",
        help="Make this the default profile for commands without --profile",
    )
    p_profile_add.add_argument(
        "--monthly-contribution",
        type=float,
        default=DEFAULT_MONTHLY_CONTRIBUTION_EUR,
        metavar="EUR",
        help=f"Planning figure for new money (default: {DEFAULT_MONTHLY_CONTRIBUTION_EUR:.0f})",
    )
    profile_sub.add_parser("list", help="List the roster")
    p_profile.set_defaults(func=cmd_profile)

    p_init = sub.add_parser("init", help="Create and seed the database")
    p_init.add_argument(
        "--snapshot",
        metavar="PATH",
        default=None,
        help="Broker snapshot TOML (default: the profile's seed_snapshot.toml)",
    )
    p_init.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the seed reconciliation without writing",
    )
    _add_db(p_init)
    p_init.set_defaults(func=cmd_init)

    p_sync = sub.add_parser("sync", help="Fetch current prices and FX rates")
    p_sync.add_argument(
        "--provider",
        metavar="NAME",
        default=None,
        help="Market-data provider (default: the configured one)",
    )
    p_sync.add_argument(
        "--prices-only",
        action="store_true",
        help="Skip the calendar and consensus fetch",
    )
    _add_db(p_sync)
    p_sync.set_defaults(func=cmd_sync)

    p_events = sub.add_parser("events", help="List known upcoming events")
    p_events.add_argument(
        "--days",
        type=int,
        default=90,
        metavar="N",
        help="How far ahead to look (default: 90)",
    )
    p_events.add_argument(
        "--past", action="store_true", help="Include events already passed"
    )
    _add_db(p_events)
    p_events.set_defaults(func=cmd_events)

    p_price = sub.add_parser("price", help="Record one price by hand")
    p_price.add_argument("ticker", help="Ticker as it appears in holdings")
    p_price.add_argument(
        "--close", type=float, required=True, metavar="AMOUNT", help="Closing price"
    )
    p_price.add_argument(
        "--currency",
        metavar="CCY",
        default=None,
        help="Currency of the price (default: the security's own)",
    )
    p_price.add_argument(
        "--date", metavar="YYYY-MM-DD", default=None, help="Date (default: today)"
    )
    _add_db(p_price)
    p_price.set_defaults(func=cmd_price)

    p_holdings = sub.add_parser("holdings", help="Show current positions")
    _add_db(p_holdings)
    p_holdings.set_defaults(func=cmd_holdings)

    p_conc = sub.add_parser("concentration", help="Show grouped portfolio weights")
    p_conc.add_argument(
        "--by",
        choices=("all", "security", "sector", "theme"),
        default="all",
        help="Grouping to report (default: all three)",
    )
    _add_db(p_conc)
    p_conc.set_defaults(func=cmd_concentration)

    p_context = sub.add_parser("context", help="Show personal context files")
    p_context.add_argument(
        "--profile",
        metavar="NAME",
        default=None,
        help="Profile name (default: the operator profile)",
    )
    p_context.set_defaults(func=cmd_context)

    p_doctor = sub.add_parser("doctor", help="Check setup readiness")
    p_doctor.add_argument(
        "--profile",
        metavar="NAME",
        default=None,
        help="Profile name (default: the operator profile)",
    )
    p_doctor.set_defaults(func=cmd_doctor)

    p_thesis = sub.add_parser("thesis", help="Why each position is held")
    thesis_sub = p_thesis.add_subparsers(dest="thesis_cmd", required=False)
    p_thesis_boot = thesis_sub.add_parser(
        "bootstrap", help="Create theses from your own notes"
    )
    p_thesis_boot.add_argument(
        "ticker", nargs="?", default=None, help="Only this holding"
    )
    p_thesis_boot.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing thesis with a new version",
    )
    _add_db(p_thesis_boot)
    p_thesis_show = thesis_sub.add_parser("show", help="Inspect one thesis in full")
    p_thesis_show.add_argument("ticker")
    _add_db(p_thesis_show)
    thesis_sub.add_parser("list", help="Every current thesis")
    _add_db(p_thesis)
    p_thesis.set_defaults(func=cmd_thesis, thesis_cmd=None, ticker=None)

    p_triage = sub.add_parser("triage", help="Rank holdings by what changed")
    p_triage.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be sent to the model, and send nothing",
    )
    _add_db(p_triage)
    p_triage.set_defaults(func=cmd_triage)

    p_research = sub.add_parser("research", help="Deep research on a holding")
    p_research.add_argument(
        "ticker",
        nargs="?",
        default=None,
        help="Holding to research (default: whatever the last triage selected)",
    )
    _add_db(p_research)
    p_research.set_defaults(func=cmd_research)

    p_recommend = sub.add_parser("recommend", help="Propose actions for the week")
    _add_db(p_recommend)
    p_recommend.set_defaults(func=cmd_recommend)

    p_decide = sub.add_parser("decide", help="Record a decision on a recommendation")
    p_decide.add_argument(
        "recommendation_id",
        nargs="?",
        type=int,
        default=None,
        help="Recommendation to decide on (omit to list them)",
    )
    p_decide.add_argument(
        "decision", nargs="?", choices=("approve", "reject", "later"), default=None
    )
    p_decide.add_argument("--note", default=None, help="Why, in your own words")
    p_decide.add_argument(
        "--limit", type=int, default=15, metavar="N", help="Rows when listing"
    )
    _add_db(p_decide)
    p_decide.set_defaults(func=cmd_decide)

    p_weekly = sub.add_parser("weekly", help="Run the whole weekly cycle")
    weekly_sub = p_weekly.add_subparsers(dest="weekly_cmd", required=False)
    weekly_sub.add_parser("install", help="Schedule it under launchd")
    weekly_sub.add_parser("stop", help="Unschedule it")
    p_weekly.add_argument(
        "--telegram", action="store_true", help="Send the report when done"
    )
    p_weekly.add_argument(
        "--max-research",
        type=int,
        default=4,
        metavar="N",
        help="Cap on deep passes (default: 4)",
    )
    p_weekly.add_argument(
        "--skip-research",
        action="store_true",
        help="Everything except the deep passes",
    )
    _add_db(p_weekly)
    p_weekly.set_defaults(func=cmd_weekly, weekly_cmd=None)

    p_report = sub.add_parser("report", help="The weekly portfolio review")
    p_report.add_argument(
        "--telegram", action="store_true", help="Send it rather than printing it"
    )
    _add_db(p_report)
    p_report.set_defaults(func=cmd_report)

    p_tg = sub.add_parser("telegram-setup", help="Register the bot command menu")
    p_tg.set_defaults(func=cmd_telegram_setup)

    p_daemon = sub.add_parser("daemon", help="Listen for Telegram replies")
    daemon_sub = p_daemon.add_subparsers(dest="daemon_cmd", required=False)
    daemon_sub.add_parser("install", help="Install and start the launchd job")
    daemon_sub.add_parser("stop", help="Stop the launchd job")
    daemon_sub.add_parser("restart", help="Restart the launchd job")
    p_daemon.set_defaults(func=cmd_daemon, daemon_cmd=None)

    p_models = sub.add_parser("models", help="Inspect or change model routing")
    models_sub = p_models.add_subparsers(dest="models_cmd", required=False)
    p_models_set = models_sub.add_parser("set", help="Route one feature")
    p_models_set.add_argument("feature", choices=FEATURES)
    p_models_set.add_argument("--model", metavar="ID", default=None, help="Model id")
    p_models_set.add_argument(
        "--temperature",
        type=float,
        default=None,
        metavar="T",
        help="Sampling temperature",
    )
    p_models_reset = models_sub.add_parser("reset", help="Restore defaults")
    p_models_reset.add_argument("feature", choices=(*FEATURES, "all"))
    p_models_cost = models_sub.add_parser("cost", help="Spend by feature")
    p_models_cost.add_argument(
        "--since", metavar="ISO", default=None, help="Only calls at or after this time"
    )
    _add_db(p_models_cost)
    _add_db(p_models)
    p_models.set_defaults(func=cmd_models, models_cmd=None)

    p_llm_log = sub.add_parser("llm-log", help="Inspect recorded model calls")
    p_llm_log.add_argument("--id", type=int, default=None, help="Show one call in full")
    p_llm_log.add_argument(
        "--trace",
        type=int,
        default=None,
        metavar="N",
        help="Every call in one operation",
    )
    p_llm_log.add_argument(
        "--feature", choices=FEATURES, default=None, help="Restrict to one stage"
    )
    p_llm_log.add_argument(
        "--errors", action="store_true", help="Only attempts that failed"
    )
    p_llm_log.add_argument(
        "--limit", type=int, default=20, metavar="N", help="Maximum rows (default: 20)"
    )
    _add_db(p_llm_log)
    p_llm_log.set_defaults(func=cmd_llm_log)

    p_db = sub.add_parser("db", help="Migration and schema admin")
    db_sub = p_db.add_subparsers(dest="db_cmd", required=True)
    for name, help_text in (
        ("status", "Row counts and migration state"),
        ("migrate", "Apply pending migrations"),
        ("schema", "Print the live schema"),
    ):
        p_db_cmd = db_sub.add_parser(name, help=help_text)
        _add_db(p_db_cmd)
    p_db.set_defaults(func=cmd_db)

    return parser


def main() -> None:
    """Entry point: parse CLI args and dispatch to the appropriate subcommand."""
    load_dotenv()
    # Set before litellm is imported anywhere: it configures its own
    # handler at import time and ignores logger levels set afterwards.
    os.environ.setdefault("LITELLM_LOG", "ERROR")
    args = build_parser().parse_args()
    setup_logging(args.verbose)

    try:
        args.func(args)
    except (
        FileNotFoundError,
        ValueError,
        ProfileConfigError,
        ProviderError,
        LLMError,
        TelegramError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
