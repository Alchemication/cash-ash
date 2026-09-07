"""Versioned SQLite migrations for CashAsh.

Migration files are named ``<UTC timestamp>__<NNN>_<slug>.py`` and expose a
module-level ``NAME`` plus an ``upgrade(conn)`` function. They are discovered
and applied in filename order, and recorded in ``schema_migrations`` so each
runs exactly once.

Public API:
    apply_migrations    -- apply everything pending, return what was applied
    list_migrations     -- report applied/pending status for each migration
    discover_migrations -- load the available migration modules in order
    get_live_schema     -- dump the database's current schema as SQL
"""

from __future__ import annotations

import importlib.util
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).parent
_SCHEMA_MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    key         TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class Migration:
    """A single versioned schema migration."""

    key: str
    name: str
    upgrade: Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class MigrationStatus:
    """Status of one available migration relative to a database."""

    key: str
    name: str
    status: str
    applied_at: str | None = None


def ensure_migration_table(conn: sqlite3.Connection) -> None:
    """Ensure the schema_migrations table exists."""
    conn.execute(_SCHEMA_MIGRATIONS_SQL)


def _migration_module_name(path: Path) -> str:
    sanitized = path.stem.replace("-", "_").replace(".", "_")
    return f"cash_ash_migration_{sanitized}"


def _load_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(_migration_module_name(path), path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load migration module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def discover_migrations() -> list[Migration]:
    """Load all available migration files in sorted order.

    Returns:
        Migrations ordered by filename, which is chronological by construction.

    Raises:
        RuntimeError: If a migration file has no callable ``upgrade``.
    """
    migrations: list[Migration] = []
    for path in sorted(_MIGRATIONS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        module = _load_module(path)
        name = getattr(module, "NAME", path.stem)
        upgrade = getattr(module, "upgrade", None)
        if not callable(upgrade):
            raise RuntimeError(f"Migration missing upgrade() function: {path.name}")
        migrations.append(Migration(key=path.stem, name=name, upgrade=upgrade))
    return migrations


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _recorded_migrations(conn: sqlite3.Connection) -> dict[str, str]:
    if not _table_exists(conn, "schema_migrations"):
        return {}
    return {
        row["key"]: row["applied_at"]
        for row in conn.execute(
            "SELECT key, applied_at FROM schema_migrations ORDER BY key"
        ).fetchall()
    }


def list_migrations(conn: sqlite3.Connection) -> list[MigrationStatus]:
    """Return available migrations with applied/pending status."""
    recorded = _recorded_migrations(conn)
    statuses: list[MigrationStatus] = []
    for migration in discover_migrations():
        if migration.key in recorded:
            statuses.append(
                MigrationStatus(
                    key=migration.key,
                    name=migration.name,
                    status="applied",
                    applied_at=recorded[migration.key],
                )
            )
        else:
            statuses.append(
                MigrationStatus(
                    key=migration.key, name=migration.name, status="pending"
                )
            )
    return statuses


def apply_migrations(conn: sqlite3.Connection) -> list[MigrationStatus]:
    """Apply all pending migrations and record them in schema_migrations.

    Each migration runs inside its own transaction together with the row that
    records it, so a failure leaves neither the schema change nor the record.

    Args:
        conn: Open connection to migrate.

    Returns:
        The migrations applied by this call, in order.
    """
    ensure_migration_table(conn)
    recorded = _recorded_migrations(conn)
    now = datetime.now(UTC).isoformat()
    applied_now: list[MigrationStatus] = []

    for migration in discover_migrations():
        if migration.key in recorded:
            continue
        logger.info("Applying migration %s", migration.key)
        with conn:
            migration.upgrade(conn)
            conn.execute(
                "INSERT INTO schema_migrations (key, name, applied_at) VALUES (?, ?, ?)",
                (migration.key, migration.name, now),
            )
        applied_now.append(
            MigrationStatus(
                key=migration.key,
                name=migration.name,
                status="applied",
                applied_at=now,
            )
        )

    return applied_now


def get_live_schema(conn: sqlite3.Connection) -> str:
    """Return the live SQLite schema as SQL text."""
    rows = conn.execute(
        """
        SELECT type, name, sql
        FROM sqlite_master
        WHERE type IN ('table', 'index', 'view')
          AND name NOT LIKE 'sqlite_%'
          AND sql IS NOT NULL
        ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'view' THEN 1 ELSE 2 END, name
        """
    ).fetchall()
    return "\n\n".join(f"{row['sql']};" for row in rows)
