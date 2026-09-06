"""skarbie — research and decision support for a small personal stock portfolio.

Subcommands:
    init           Create the database and seed it from a broker snapshot file.
    holdings       Show current positions, cost basis, value and P&L.
    concentration  Show grouped weights by security, sector and theme.
    db             Migration and schema admin.

Examples:
    uv run python main.py init --dry-run
        Check the snapshot reconciles against the broker's stated total,
        writing nothing.

    uv run python main.py init
        Create and seed the database.

    uv run python main.py holdings
        Current positions valued at the latest known prices.

    uv run python main.py concentration --by theme
        Theme weights, flagging anything over the configured limit.

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
from commands import cmd_concentration, cmd_holdings, cmd_init  # noqa: E402
from config import DB_PATH, SEED_SNAPSHOT_PATH  # noqa: E402
from log import setup_logging  # noqa: E402


def main() -> None:
    """Entry point: parse CLI args and dispatch to the appropriate subcommand."""
    load_dotenv()

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
            "--db",
            metavar="PATH",
            default=str(DB_PATH),
            help=f"SQLite database path (default: {DB_PATH})",
        )

    p_init = sub.add_parser("init", help="Create and seed the database")
    p_init.add_argument(
        "--snapshot",
        metavar="PATH",
        default=None,
        help=f"Broker snapshot TOML (default: {SEED_SNAPSHOT_PATH})",
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

    args = parser.parse_args()
    setup_logging(args.verbose)

    try:
        args.func(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
