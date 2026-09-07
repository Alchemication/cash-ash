"""Persist review health, research coverage and verifiable evidence packages."""

import sqlite3

NAME = "review workflow and evidence provenance"


def upgrade(conn: sqlite3.Connection) -> None:
    """Add workflow records without inferring success for historical runs."""
    conn.executescript(
        """
        CREATE TABLE review_cycle (
            id INTEGER PRIMARY KEY,
            run_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('running','complete','incomplete')),
            stages_json TEXT NOT NULL DEFAULT '[]',
            started_at TEXT NOT NULL,
            finished_at TEXT
        );
        CREATE TABLE research_assessment (
            run_id INTEGER PRIMARY KEY REFERENCES research_run(id),
            security_id INTEGER NOT NULL REFERENCES securities(id),
            thesis_id INTEGER REFERENCES thesis(id),
            status TEXT NOT NULL,
            reason TEXT NOT NULL,
            questions_json TEXT NOT NULL,
            answers_json TEXT NOT NULL,
            triggered_json TEXT NOT NULL,
            open_questions_json TEXT NOT NULL,
            package_json TEXT NOT NULL,
            package_hash TEXT NOT NULL,
            coverage TEXT NOT NULL CHECK(coverage IN ('sufficient','insufficient')),
            created_at TEXT NOT NULL
        );
        CREATE TABLE decision_batch (
            run_id INTEGER PRIMARY KEY REFERENCES research_run(id),
            summary TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        ALTER TABLE user_decision ADD COLUMN snoozed_until TEXT;
        ALTER TABLE user_decision ADD COLUMN reminder_sent_at TEXT;
        CREATE TABLE decision_refusal (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL REFERENCES research_run(id),
            ticker TEXT,
            action TEXT NOT NULL,
            rationale TEXT NOT NULL,
            refusal TEXT NOT NULL
        );
        ALTER TABLE execution ADD COLUMN outcome TEXT NOT NULL DEFAULT 'executed'
            CHECK(outcome IN ('executed','not_executed'));
        CREATE UNIQUE INDEX execution_once ON execution(recommendation_id)
            WHERE recommendation_id IS NOT NULL;
        CREATE TABLE evidence_new (
            id INTEGER PRIMARY KEY,
            research_run_id INTEGER REFERENCES research_run(id) ON DELETE CASCADE,
            security_id INTEGER REFERENCES securities(id) ON DELETE CASCADE,
            claim TEXT NOT NULL,
            source_url TEXT,
            source_title TEXT,
            published_date TEXT,
            strength TEXT NOT NULL DEFAULT 'unstated'
                CHECK(strength IN ('unstated','weak','moderate','strong')),
            kind TEXT NOT NULL DEFAULT 'background' CHECK(kind IN ('sourced','background','unanswered')),
            created_at TEXT NOT NULL,
            CHECK(kind <> 'sourced' OR (source_url IS NOT NULL AND published_date IS NOT NULL))
        );
        INSERT INTO evidence_new SELECT * FROM evidence;
        DROP TABLE evidence;
        ALTER TABLE evidence_new RENAME TO evidence;
        CREATE INDEX evidence_run ON evidence(research_run_id);
        CREATE INDEX evidence_security ON evidence(security_id);
        CREATE INDEX evidence_kind ON evidence(kind);
        """
    )
