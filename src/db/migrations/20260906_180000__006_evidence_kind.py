"""Force every claim to declare whether it has a source or is model knowledge."""

from __future__ import annotations

import sqlite3

NAME = "evidence provenance kinds"


def upgrade(conn: sqlite3.Connection) -> None:
    """Constrain evidence.kind and require a source where one is claimed.

    Two kinds of claim reach an analysis and they are not interchangeable.

    ``sourced`` is anchored to something published: it carries a URL and a
    date, and a trigger refuses it without them. Anything time-sensitive —
    what a company just reported, how a market moved, what was announced last
    week — must be this kind, because a model's recollection of recent events
    is exactly where it is least reliable and most confident.

    ``background`` is the model's own knowledge, and it is legitimate: how an
    industry works, what happened years ago, what a pattern usually implies.
    It is useful context and it is allowed. What it may not do is masquerade as
    a current fact, which is why the distinction is a column rather than an
    instruction in a prompt — a prompt can be ignored, a CHECK cannot.

    SQLite cannot add a CHECK to an existing table, so this rebuilds it. The
    table is empty at this point in the project's life; the copy is here so the
    migration is still correct if it is not.
    """
    conn.executescript(
        """
        CREATE TABLE evidence_new (
            id              INTEGER PRIMARY KEY,
            research_run_id INTEGER REFERENCES research_run(id) ON DELETE CASCADE,
            security_id     INTEGER REFERENCES securities(id) ON DELETE CASCADE,
            claim           TEXT NOT NULL,
            source_url      TEXT,
            source_title    TEXT,
            published_date  TEXT,
            strength        TEXT NOT NULL DEFAULT 'unstated'
                            CHECK (strength IN
                                ('unstated', 'weak', 'moderate', 'strong')),
            kind            TEXT NOT NULL DEFAULT 'background'
                            CHECK (kind IN ('sourced', 'background')),
            created_at      TEXT NOT NULL,
            CHECK (
                kind <> 'sourced'
                OR (source_url IS NOT NULL AND published_date IS NOT NULL)
            )
        );

        INSERT INTO evidence_new (
            id, research_run_id, security_id, claim, source_url, source_title,
            published_date, strength, kind, created_at
        )
        SELECT
            id, research_run_id, security_id, claim, source_url, source_title,
            published_date, strength,
            CASE
                WHEN source_url IS NOT NULL AND published_date IS NOT NULL
                THEN 'sourced' ELSE 'background'
            END,
            created_at
        FROM evidence;

        DROP TABLE evidence;
        ALTER TABLE evidence_new RENAME TO evidence;

        CREATE INDEX IF NOT EXISTS evidence_run ON evidence(research_run_id);
        CREATE INDEX IF NOT EXISTS evidence_security ON evidence(security_id);
        CREATE INDEX IF NOT EXISTS evidence_kind ON evidence(kind);
        """
    )
