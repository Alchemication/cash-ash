"""Record every model call: what was sent, what came back, what it cost."""

from __future__ import annotations

import sqlite3

NAME = "llm call and trace logging"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create the llm_trace and llm_call tables.

    A trace groups the calls belonging to one logical operation — a weekly run
    fans out across triage, several analysts and a synthesis, and reading any
    one of those in isolation says little about why the last one concluded what
    it did.

    Everything needed to evaluate a model later is stored at call time, because
    none of it is recoverable afterwards: the model id, the prompt version, the
    reasoning the model emitted, the token counts and the cost. Model routing
    changes, prompts get edited, and providers retire model ids; a result
    recorded without them cannot be attributed.

    Messages and responses are stored as JSON text rather than parsed columns.
    They are read by a human debugging one call, never queried across calls,
    and a schema for them would have to change every time a prompt does.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS llm_trace (
            id          INTEGER PRIMARY KEY,
            operation   TEXT NOT NULL,
            feature     TEXT,
            reference   TEXT,
            started_at  TEXT NOT NULL,
            note        TEXT
        );

        CREATE INDEX IF NOT EXISTS llm_trace_started ON llm_trace(started_at);

        CREATE TABLE IF NOT EXISTS llm_call (
            id                INTEGER PRIMARY KEY,
            -- No foreign key on purpose: this is a pointer for llm-log, and a
            -- constraint here would mean a research result could fail to save
            -- because its call log did not.
            trace_id          INTEGER,
            feature           TEXT NOT NULL,
            model             TEXT NOT NULL,
            requested_model   TEXT NOT NULL,
            attempt           INTEGER NOT NULL DEFAULT 1,
            prompt_version    TEXT,
            messages_json     TEXT NOT NULL,
            response_text     TEXT,
            reasoning_text    TEXT,
            finish_reason     TEXT,
            input_tokens      INTEGER,
            output_tokens     INTEGER,
            cost_usd          REAL,
            latency_s         REAL,
            max_tokens        INTEGER,
            temperature       REAL,
            error             TEXT,
            created_at        TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS llm_call_trace ON llm_call(trace_id);
        CREATE INDEX IF NOT EXISTS llm_call_created ON llm_call(created_at);
        CREATE INDEX IF NOT EXISTS llm_call_feature ON llm_call(feature, created_at);
        """
    )
