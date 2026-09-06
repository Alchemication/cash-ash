"""Database admin subcommand: migrations, schema, status.

Kept out of ``commands.py`` from the start, following the same split zdrowskit
arrived at once that module outgrew a single file.
"""

from __future__ import annotations

import argparse

from db.migrations import apply_migrations, get_live_schema, list_migrations
from profiles import resolve_cli_profile
from store import connect_db


def _table_names(conn) -> list[str]:
    """Return the database's own table names, newest schema included.

    Read from the database rather than listed here: a hardcoded roster silently
    stops mentioning whatever the most recent migration added, which is exactly
    when someone is looking.
    """
    return [
        row[0]
        for row in conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
              AND name != 'schema_migrations'
            ORDER BY name
            """
        ).fetchall()
    ]


def _format_bytes(size_bytes: int) -> str:
    """Format a byte count into a compact human-readable string."""
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def cmd_db(args: argparse.Namespace) -> None:
    """Handle the 'db' subcommand family for migration and schema admin.

    Raises:
        FileNotFoundError: If the resolved database does not exist.
        ProfileConfigError: If the named profile is unknown.
    """
    from rich.console import Console
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table

    console = Console()
    profile, db_path = resolve_cli_profile(
        args.profile, db=args.db, require_existing=False
    )
    db_path = db_path.expanduser().resolve()

    if not db_path.exists():
        target = f" --profile {profile.name}" if profile else ""
        raise FileNotFoundError(
            f"No database at {db_path}. Run 'uv run python main.py init"
            f"{target}' to create and seed it; 'db' will not create one "
            f"implicitly."
        )

    if args.db_cmd == "migrate":
        conn = connect_db(db_path, migrate=False)
        changes = apply_migrations(conn)
        if not changes:
            console.print(
                Panel(
                    "Database schema is already up to date.",
                    title="DB Migrate",
                    border_style="green",
                )
            )
            return
        table = Table(title="Applied Migrations")
        table.add_column("Status", style="cyan", no_wrap=True)
        table.add_column("Key", style="magenta", overflow="fold")
        table.add_column("Name")
        for change in changes:
            table.add_row(change.status, change.key, change.name)
        console.print(table)
        return

    if args.db_cmd == "schema":
        conn = connect_db(db_path, migrate=False)
        console.print(Syntax(get_live_schema(conn), "sql", theme="ansi_dark"))
        return

    conn = connect_db(db_path, migrate=False)
    table = Table(title=f"Database: {db_path}")
    table.add_column("Table", style="cyan")
    table.add_column("Rows", justify="right")
    for name in _table_names(conn):
        row = conn.execute(f'SELECT COUNT(*) AS n FROM "{name}"').fetchone()
        table.add_row(name, f"{row['n']:,}")
    console.print(table)

    statuses = list_migrations(conn)
    pending = [status for status in statuses if status.status == "pending"]
    console.print(
        f"Size [bold]{_format_bytes(db_path.stat().st_size)}[/bold] · "
        f"Migrations [bold]{len(statuses) - len(pending)}/{len(statuses)}[/bold] applied"
    )
    if pending:
        console.print(
            f"[yellow]{len(pending)} pending. Run "
            f"'uv run python main.py db migrate'.[/yellow]"
        )
