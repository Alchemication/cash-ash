"""skarbie — research and decision support for a small personal stock portfolio.

Subcommands:
    profile        Create and list profiles — one per person.
    init           Create the database and seed it from a broker snapshot file.
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

    uv run python main.py holdings --profile kasia
        Someone else's portfolio.

    uv run python main.py doctor
        What is set up and what is still missing.

    uv run python main.py db status
        Row counts and migration state.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

# Ensure src/ is on the path when running from the project root
sys.path.insert(0, str(Path(__file__).parent / "src"))

from cmd_db import cmd_db  # noqa: E402
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
    args = build_parser().parse_args()
    setup_logging(args.verbose)

    try:
        args.func(args)
    except (FileNotFoundError, ValueError, ProfileConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
