"""Let a ledger row name the statement row it came from."""

from __future__ import annotations

import sqlite3

NAME = "statement source references"


def upgrade(conn: sqlite3.Connection) -> None:
    """Add ``source_ref`` to trades and cash flows, unique where present.

    Statements overlap: a September statement and a year-to-date one both
    contain the same fill, and importing a month twice is the normal way to
    recover from a failed download. Without an identity for the row, each
    import would add the trade again and quietly double the position.

    The reference is derived from the statement row itself — its timestamp,
    symbol, side and quantity — rather than from a broker id, because the PDF
    states no id. It is unique only where it is set: trades entered by hand or
    by the seed carry none, and several of those may look alike.
    """
    conn.executescript(
        """
        ALTER TABLE trades ADD COLUMN source_ref TEXT;
        ALTER TABLE cash_flows ADD COLUMN source_ref TEXT;

        CREATE UNIQUE INDEX IF NOT EXISTS trades_source_ref
            ON trades(source_ref) WHERE source_ref IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS cash_flows_source_ref
            ON cash_flows(source_ref) WHERE source_ref IS NOT NULL;
        """
    )
