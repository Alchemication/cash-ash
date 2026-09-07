"""Let research propose a thesis revision without adopting it."""

from __future__ import annotations

import sqlite3

NAME = "proposed thesis revisions"


def upgrade(conn: sqlite3.Connection) -> None:
    """Add a 'proposed' thesis status and allow more than one per security.

    Research may conclude that a thesis has weakened, improved or broken. It
    may not act on that conclusion. A proposal sits beside the active thesis
    until the owner accepts it, because the thesis is a record of what *they*
    believe, and a pipeline that could rewrite it would be editing the baseline
    it is measured against.

    The original UNIQUE(security_id, version) still holds — a proposal takes
    the next version number and simply never becomes active on its own. What
    changes is that the active-thesis lookup must filter on status rather than
    assuming one row per security, which it already does.

    SQLite cannot widen a CHECK in place, so the table is rebuilt.
    """
    conn.executescript(
        """
        CREATE TABLE thesis_new (
            id                  INTEGER PRIMARY KEY,
            security_id         INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
            version             INTEGER NOT NULL,
            status              TEXT NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active', 'superseded',
                                                  'proposed', 'rejected')),
            summary             TEXT NOT NULL,
            rationale           TEXT,
            conviction          TEXT NOT NULL DEFAULT 'unstated'
                                CHECK (conviction IN
                                    ('unstated', 'none', 'weak', 'moderate', 'strong')),
            thesis_status       TEXT NOT NULL DEFAULT 'unexamined'
                                CHECK (thesis_status IN
                                    ('unexamined', 'improving', 'unchanged',
                                     'deteriorating', 'broken')),
            key_assumptions     TEXT,
            open_questions      TEXT,
            what_would_break_it TEXT,
            source              TEXT NOT NULL
                                CHECK (source IN ('user', 'research')),
            supersedes_id       INTEGER REFERENCES thesis(id),
            llm_call_id         INTEGER,
            research_run_id     INTEGER,
            note                TEXT,
            created_at          TEXT NOT NULL,
            UNIQUE (security_id, version)
        );

        INSERT INTO thesis_new (
            id, security_id, version, status, summary, rationale, conviction,
            thesis_status, key_assumptions, open_questions, what_would_break_it,
            source, supersedes_id, llm_call_id, note, created_at
        )
        SELECT
            id, security_id, version, status, summary, rationale, conviction,
            thesis_status, key_assumptions, open_questions, what_would_break_it,
            source, supersedes_id, llm_call_id, note, created_at
        FROM thesis;

        DROP TABLE thesis;
        ALTER TABLE thesis_new RENAME TO thesis;

        CREATE INDEX IF NOT EXISTS thesis_active ON thesis(security_id, status);
        """
    )
