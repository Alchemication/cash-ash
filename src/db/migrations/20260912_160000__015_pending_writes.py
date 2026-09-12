"""Proposals the owner has not yet confirmed.

The chat agent never writes. It proposes, and a button confirms — so something
has to hold the proposal in between, and that something cannot be memory: a
daemon restarted between the question and the tap would silently lose a
contribution the owner believes they recorded. Nothing about a money write
should depend on a process staying up.

The row also answers a question worth answering later: what was proposed, what
was confirmed, and what was quietly left to expire.
"""

import sqlite3

NAME = "pending writes awaiting confirmation from chat"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create the proposal table."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pending_write (
            id            INTEGER PRIMARY KEY,
            kind          TEXT NOT NULL CHECK (kind IN (
                              'cash_flow', 'context_note'
                          )),
            -- The arguments as validated at proposal time, not as the model
            -- first phrased them. Applying re-reads this and nothing else, so a
            -- confirmation cannot mean something different from what was shown.
            payload_json  TEXT NOT NULL,
            -- Exactly the sentence the owner was asked to confirm. Kept
            -- verbatim because it is the record of what they agreed to.
            summary       TEXT NOT NULL,
            source        TEXT NOT NULL DEFAULT 'chat',
            proposed_at   TEXT NOT NULL,
            expires_at    TEXT NOT NULL,
            resolved_at   TEXT,
            resolution    TEXT CHECK (resolution IN (
                              'confirmed', 'cancelled', 'expired'
                          )),
            -- What the write produced, for a confirmed proposal: the cash_flows
            -- row id, or the file written. Without it there is no way back from
            -- a confirmation to its effect.
            result        TEXT
        );

        CREATE INDEX IF NOT EXISTS pending_write_open
            ON pending_write(resolved_at, expires_at);
        """
    )
