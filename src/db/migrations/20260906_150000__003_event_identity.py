"""Make event identity work for events that belong to no single holding."""

from __future__ import annotations

import sqlite3

NAME = "event identity for macro events"


def upgrade(conn: sqlite3.Connection) -> None:
    """Add a uniqueness index that treats a missing security as a value.

    Migration 002 declared UNIQUE (security_id, kind, event_date, source,
    title). SQLite considers two NULLs distinct in a unique index, so a macro
    event — a rate decision, an inflation print, anything belonging to the
    portfolio rather than one holding — never collided with itself and gained a
    fresh duplicate on every weekly sync.

    COALESCE folds the missing security to a sentinel that cannot be a real row
    id, restoring the intended identity. The original constraint is left in
    place: for a non-null security it says exactly the same thing, so it costs
    an index and contradicts nothing.

    Existing duplicates are collapsed first, keeping the lowest id, or the new
    index could not be built.
    """
    conn.executescript(
        """
        DELETE FROM events
        WHERE id NOT IN (
            SELECT MIN(id) FROM events
            GROUP BY COALESCE(security_id, -1), kind, event_date, source, title
        );

        CREATE UNIQUE INDEX IF NOT EXISTS events_identity
            ON events (
                COALESCE(security_id, -1), kind, event_date, source, title
            );
        """
    )
