"""Recommendations, the owner's response, and what was eventually executed."""

from __future__ import annotations

import sqlite3

NAME = "recommendations, decisions and executions"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create the recommendation, user_decision and execution tables.

    Three tables rather than one because they are three different facts, and
    conflating them destroys the only comparison worth making later: what was
    recommended, what the owner decided, and what actually happened. A single
    row with a status column would lose the recommendations that were rejected,
    which are exactly the ones an evaluation needs.

    ``price_native`` and ``fx_rate`` are captured at recommendation time. This
    is the forward-tracking the whole evaluation rests on and it cannot be
    reconstructed later — a price is only knowable as of a moment, and by the
    time anyone asks how a recommendation did, that moment is gone.

    ``guardrails`` records which deterministic checks ran and what they
    concluded. When a recommendation was clamped or refused, the reason has to
    survive: "the model wanted more than the rules allow" is a different event
    from "the model asked for this amount".

    ``expires_on`` exists because a weekly cadence supersedes itself. Approving
    a recommendation five days late executes research that has been replaced,
    at a price that has moved.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS recommendation (
            id                INTEGER PRIMARY KEY,
            run_date          TEXT NOT NULL,
            research_run_id   INTEGER REFERENCES research_run(id),
            -- Null for a portfolio-level recommendation such as keeping cash.
            security_id       INTEGER REFERENCES securities(id),
            action            TEXT NOT NULL CHECK (action IN
                                ('BUY', 'ADD', 'HOLD', 'TRIM', 'EXIT',
                                 'REVIEW', 'KEEP_CASH')),
            amount_eur        REAL,
            rationale         TEXT NOT NULL,
            urgency           TEXT NOT NULL DEFAULT 'low'
                              CHECK (urgency IN ('low', 'medium', 'high')),
            thesis_id         INTEGER REFERENCES thesis(id),
            price_native      REAL,
            fx_rate           REAL,
            value_eur         REAL,
            weight_pct        REAL,
            expires_on        TEXT NOT NULL,
            guardrails        TEXT,
            adjusted          INTEGER NOT NULL DEFAULT 0,
            llm_call_id       INTEGER,
            created_at        TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS recommendation_date
            ON recommendation(run_date);
        CREATE INDEX IF NOT EXISTS recommendation_security
            ON recommendation(security_id, run_date);

        CREATE TABLE IF NOT EXISTS user_decision (
            id                INTEGER PRIMARY KEY,
            recommendation_id INTEGER NOT NULL
                              REFERENCES recommendation(id) ON DELETE CASCADE,
            decision          TEXT NOT NULL CHECK (decision IN
                                ('approve', 'reject', 'later')),
            note              TEXT,
            decided_at        TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS user_decision_recommendation
            ON user_decision(recommendation_id);

        -- Separate from the decision on purpose: approving is not executing.
        -- The owner's strategy makes the gap deliberate, and collapsing the
        -- two would erase the evidence of whether they acted at all.
        CREATE TABLE IF NOT EXISTS execution (
            id                INTEGER PRIMARY KEY,
            recommendation_id INTEGER REFERENCES recommendation(id),
            trade_id          INTEGER REFERENCES trades(id),
            executed_on       TEXT NOT NULL,
            quantity          REAL,
            amount_eur        REAL,
            note              TEXT,
            created_at        TEXT NOT NULL
        );
        """
    )
