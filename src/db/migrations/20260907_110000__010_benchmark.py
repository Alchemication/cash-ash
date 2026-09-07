"""Track a passive index alongside the portfolio."""

from __future__ import annotations

import sqlite3

NAME = "passive benchmark"


def upgrade(conn: sqlite3.Connection) -> None:
    """Mark securities held only for comparison.

    A benchmark is a security with prices and no trades. The flag exists
    because price syncing asks what is *held*, and a benchmark is deliberately
    not held — without it the one security whose history the comparison depends
    on would be the only one never fetched.

    Kept in ``securities`` rather than a table of its own so that everything
    already built for prices, currencies and feed symbols applies unchanged.
    Nothing derives a position from it, because a position comes from trades
    and it has none.
    """
    conn.executescript(
        """
        ALTER TABLE securities ADD COLUMN is_benchmark INTEGER NOT NULL DEFAULT 0;

        CREATE INDEX IF NOT EXISTS securities_benchmark
            ON securities(is_benchmark);
        """
    )
