"""Versioned investment theses: why a position is held, and what would break it."""

from __future__ import annotations

import sqlite3

NAME = "versioned theses and research runs"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create the thesis, research_run and evidence tables.

    A thesis is never updated in place. Each revision is a new row with a
    higher version, and the previous one is marked superseded, because the
    whole point is to be able to ask a year later what was believed at the time
    and how it changed. Overwriting would destroy the only record of that.

    ``what_would_break_it`` is the field that makes the rest work. A statement
    that cannot be falsified is not a thesis, it is a preference, and nothing
    downstream can detect that it stopped being true. Triage compares incoming
    events against these conditions.

    Conviction is an ordinal label, never a number. An LLM's "confidence: 91%"
    is not a calibrated probability, and storing it as one would launder a
    guess into a statistic. The same applies to a person's own certainty.

    ``source`` distinguishes what the owner said from what the pipeline
    concluded. The first is ground truth about intent; the second is an opinion
    that happens to be stored in the same table.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS thesis (
            id                  INTEGER PRIMARY KEY,
            security_id         INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
            version             INTEGER NOT NULL,
            status              TEXT NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active', 'superseded')),
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
            note                TEXT,
            created_at          TEXT NOT NULL,
            UNIQUE (security_id, version)
        );

        CREATE INDEX IF NOT EXISTS thesis_active
            ON thesis(security_id, status);

        CREATE TABLE IF NOT EXISTS research_run (
            id          INTEGER PRIMARY KEY,
            run_date    TEXT NOT NULL,
            kind        TEXT NOT NULL CHECK (kind IN ('bootstrap', 'triage', 'deep')),
            trace_id    INTEGER,
            note        TEXT,
            created_at  TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS research_run_date ON research_run(run_date);

        -- One row per holding considered in a triage pass. Every holding is
        -- recorded, including the ones passed over: "nothing needed looking at
        -- this week" is a finding, and it is invisible if only the selected
        -- ones are stored.
        CREATE TABLE IF NOT EXISTS triage_result (
            run_id       INTEGER NOT NULL REFERENCES research_run(id) ON DELETE CASCADE,
            security_id  INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
            rank         INTEGER NOT NULL,
            selected     INTEGER NOT NULL DEFAULT 0,
            reason       TEXT NOT NULL,
            signals      TEXT,
            PRIMARY KEY (run_id, security_id)
        );

        CREATE TABLE IF NOT EXISTS evidence (
            id              INTEGER PRIMARY KEY,
            research_run_id INTEGER REFERENCES research_run(id) ON DELETE CASCADE,
            security_id     INTEGER REFERENCES securities(id) ON DELETE CASCADE,
            claim           TEXT NOT NULL,
            source_url      TEXT,
            source_title    TEXT,
            published_date  TEXT,
            -- How well the source supports the claim, as an ordinal label.
            -- Never a percentage: see the note on conviction above.
            strength        TEXT NOT NULL DEFAULT 'unstated'
                            CHECK (strength IN
                                ('unstated', 'weak', 'moderate', 'strong')),
            kind            TEXT,
            created_at      TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS evidence_run ON evidence(research_run_id);
        CREATE INDEX IF NOT EXISTS evidence_security ON evidence(security_id);
        """
    )
