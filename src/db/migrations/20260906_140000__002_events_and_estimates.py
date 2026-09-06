"""Known future dates, and a time series of analyst consensus."""

from __future__ import annotations

import sqlite3

NAME = "events and consensus estimates"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create the events and consensus_estimates tables.

    ``events`` holds dates worth knowing in advance, from three kinds of
    source. ``feed`` is fetched and authoritative — earnings, ex-dividend.
    ``curated`` is hand-maintained for the many events no API carries: a
    keynote, an IPO lockup expiry, a delivery report. ``research`` is written
    by the pipeline when a model finds a date, and is deliberately the least
    trusted of the three so triage can weight it accordingly.

    ``security_id`` is nullable because a macro event — a rate decision, an
    inflation print — belongs to the portfolio rather than to one holding.

    ``consensus_estimates`` is keyed by *observed* date, not by fiscal period.
    The provider reports what consensus is today and never what it was last
    month, so the only way to detect a revision is to keep asking and store
    each answer. Recording starts before anything consumes it precisely
    because this series cannot be backfilled.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS events (
            id           INTEGER PRIMARY KEY,
            security_id  INTEGER REFERENCES securities(id) ON DELETE CASCADE,
            event_date   TEXT NOT NULL,
            kind         TEXT NOT NULL,
            title        TEXT NOT NULL,
            source       TEXT NOT NULL CHECK (source IN ('feed', 'curated', 'research')),
            confidence   TEXT NOT NULL DEFAULT 'confirmed'
                         CHECK (confidence IN ('confirmed', 'estimated')),
            note         TEXT,
            created_at   TEXT NOT NULL,
            UNIQUE (security_id, kind, event_date, source, title)
        );

        CREATE INDEX IF NOT EXISTS events_date ON events(event_date);
        CREATE INDEX IF NOT EXISTS events_security ON events(security_id, event_date);

        CREATE TABLE IF NOT EXISTS consensus_estimates (
            security_id    INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
            observed_date  TEXT NOT NULL,
            period_end     TEXT,
            eps_avg        REAL,
            eps_low        REAL,
            eps_high       REAL,
            revenue_avg    REAL,
            revenue_low    REAL,
            revenue_high   REAL,
            source         TEXT NOT NULL,
            fetched_at     TEXT NOT NULL,
            PRIMARY KEY (security_id, observed_date)
        );
        """
    )
