"""Distinguish a recommendation that lapsed from one a rerun replaced."""

from __future__ import annotations

import sqlite3

NAME = "superseded recommendations"


def upgrade(conn: sqlite3.Connection) -> None:
    """Record which run superseded a recommendation, when one did.

    Two decision runs on the same day produced two overlapping sets, and both
    showed as pending because the report filtered on the latest date rather
    than the latest run. The owner received the same recommendation twice.

    Rerunning is not an edge case here: the weekly cycle deliberately continues
    past a failed stage, so re-running after fixing something is the normal
    repair. The second run has to retire the first run's unanswered
    recommendations.

    A column rather than backdating ``expires_on``, because the two are
    different facts and later evaluation needs to tell them apart. A
    recommendation the owner let lapse says something about the recommendation;
    one replaced by a rerun says nothing at all, and counting the second as the
    first would quietly understate how often advice was acted on.
    """
    conn.executescript(
        """
        ALTER TABLE recommendation ADD COLUMN superseded_by_run_id INTEGER;

        CREATE INDEX IF NOT EXISTS recommendation_live
            ON recommendation(run_date, superseded_by_run_id);
        """
    )
