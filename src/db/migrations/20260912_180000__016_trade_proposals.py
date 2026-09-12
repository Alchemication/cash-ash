"""Let a proposed write be a trade.

``pending_write.kind`` is constrained to the kinds that existed when the table
was created, and SQLite cannot alter a CHECK in place — the table is rebuilt.
The constraint is worth keeping rather than dropping: it is what stops a typo in
a new proposal kind from being stored and then failing at the moment somebody
taps Confirm, which is the worst possible time to discover it.
"""

import sqlite3

NAME = "allow a trade to be proposed"


def upgrade(conn: sqlite3.Connection) -> None:
    """Rebuild pending_write with 'trade' among the allowed kinds."""
    conn.executescript(
        """
        CREATE TABLE pending_write_new (
            id            INTEGER PRIMARY KEY,
            kind          TEXT NOT NULL CHECK (kind IN (
                              'cash_flow', 'context_note', 'trade'
                          )),
            payload_json  TEXT NOT NULL,
            summary       TEXT NOT NULL,
            source        TEXT NOT NULL DEFAULT 'chat',
            proposed_at   TEXT NOT NULL,
            expires_at    TEXT NOT NULL,
            resolved_at   TEXT,
            resolution    TEXT CHECK (resolution IN (
                              'confirmed', 'cancelled', 'expired'
                          )),
            result        TEXT
        );

        INSERT INTO pending_write_new
            SELECT id, kind, payload_json, summary, source, proposed_at,
                   expires_at, resolved_at, resolution, result
            FROM pending_write;

        DROP TABLE pending_write;
        ALTER TABLE pending_write_new RENAME TO pending_write;

        CREATE INDEX IF NOT EXISTS pending_write_open
            ON pending_write(resolved_at, expires_at);
        """
    )
