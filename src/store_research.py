"""Persistence for the research layer: model calls, theses, research runs.

Split from :mod:`store`, which owns the portfolio ledger. The two touch the
same database but answer different questions — what the portfolio is, versus
what has been thought about it — and keeping them apart also keeps each file
inside the size the project holds itself to.

Public API:
    create_llm_trace    -- open a trace grouping one operation's calls
    log_llm_call        -- record one model call attempt
    load_llm_calls      -- recent calls, optionally filtered
    llm_cost_summary    -- spend and call counts by feature
    save_thesis         -- record a new thesis version, superseding the last
    active_thesis       -- the current thesis for one security
    active_theses       -- current theses for every security that has one
    thesis_history      -- every version for one security, newest first
    create_research_run -- open a research run

Example:
    from store_research import active_thesis

    thesis = active_thesis(conn, security_id=3)
"""

from __future__ import annotations

import json
import logging
import sqlite3

from models import Thesis
from store import _now

logger = logging.getLogger(__name__)


def create_llm_trace(
    conn: sqlite3.Connection,
    *,
    operation: str,
    feature: str | None = None,
    reference: str | None = None,
    note: str | None = None,
) -> int:
    """Open a trace grouping the calls belonging to one operation.

    Args:
        conn: Open database connection.
        operation: What is running, e.g. ``weekly_run``.
        feature: The stage, when a trace covers only one.
        reference: What it concerns, e.g. a ticker.
        note: Optional free text.

    Returns:
        The trace id, to pass to each call.
    """
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO llm_trace (operation, feature, reference, started_at, note)
            VALUES (?, ?, ?, ?, ?)
            """,
            (operation, feature, reference, _now(), note),
        )
    return int(cursor.lastrowid)


def log_llm_call(
    conn: sqlite3.Connection,
    *,
    feature: str,
    model: str,
    requested_model: str,
    messages_json: str,
    trace_id: int | None = None,
    attempt: int = 1,
    prompt_version: str | None = None,
    response_text: str | None = None,
    reasoning_text: str | None = None,
    finish_reason: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
    latency_s: float | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    error: str | None = None,
) -> int:
    """Record one model call attempt, successful or not.

    Failed attempts are stored too: a model that fails repeatedly is exactly
    what a later evaluation needs to see, and it is invisible if only
    successes are kept.

    Returns:
        The call's row id.
    """
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO llm_call (
                trace_id, feature, model, requested_model, attempt, prompt_version,
                messages_json, response_text, reasoning_text, finish_reason,
                input_tokens, output_tokens, cost_usd, latency_s, max_tokens,
                temperature, error, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trace_id,
                feature,
                model,
                requested_model,
                attempt,
                prompt_version,
                messages_json,
                response_text,
                reasoning_text,
                finish_reason,
                input_tokens,
                output_tokens,
                cost_usd,
                latency_s,
                max_tokens,
                temperature,
                error,
                _now(),
            ),
        )
    return int(cursor.lastrowid)


def load_llm_calls(
    conn: sqlite3.Connection,
    *,
    call_id: int | None = None,
    trace_id: int | None = None,
    feature: str | None = None,
    errors_only: bool = False,
    limit: int = 20,
) -> list[sqlite3.Row]:
    """Return recorded calls, newest first.

    Args:
        conn: Open database connection.
        call_id: One specific call.
        trace_id: Every call in one operation, oldest first.
        feature: Restrict to one stage.
        errors_only: Only attempts that failed.
        limit: Maximum rows.

    Returns:
        Matching call rows.
    """
    clauses: list[str] = []
    params: list[object] = []
    if call_id is not None:
        clauses.append("id = ?")
        params.append(call_id)
    if trace_id is not None:
        clauses.append("trace_id = ?")
        params.append(trace_id)
    if feature:
        clauses.append("feature = ?")
        params.append(feature)
    if errors_only:
        clauses.append("error IS NOT NULL")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    # A trace reads as a sequence, so it runs forwards; everything else is a
    # "what happened recently" question and runs backwards.
    order = "id ASC" if trace_id is not None else "id DESC"
    params.append(limit)
    return conn.execute(
        f"SELECT * FROM llm_call {where} ORDER BY {order} LIMIT ?", params
    ).fetchall()


def llm_cost_summary(
    conn: sqlite3.Connection, *, since: str | None = None
) -> list[sqlite3.Row]:
    """Return spend, call counts and failures by feature.

    Args:
        conn: Open database connection.
        since: Only calls created on or after this ISO timestamp.

    Returns:
        One row per feature, costliest first.
    """
    where = "WHERE created_at >= ?" if since else ""
    params = (since,) if since else ()
    return conn.execute(
        f"""
        SELECT
            feature,
            COUNT(*) AS calls,
            SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS failures,
            COALESCE(SUM(cost_usd), 0) AS cost_usd,
            COALESCE(SUM(input_tokens), 0) AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens
        FROM llm_call
        {where}
        GROUP BY feature
        ORDER BY cost_usd DESC
        """,
        params,
    ).fetchall()


def _thesis_from_row(row: sqlite3.Row) -> Thesis:
    """Rebuild a Thesis from a database row."""

    def listed(value: str | None) -> tuple[str, ...]:
        if not value:
            return ()
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return ()
        return tuple(str(item) for item in parsed) if isinstance(parsed, list) else ()

    return Thesis(
        id=int(row["id"]),
        security_id=int(row["security_id"]),
        version=int(row["version"]),
        status=row["status"],
        summary=row["summary"],
        rationale=row["rationale"],
        conviction=row["conviction"],
        thesis_status=row["thesis_status"],
        key_assumptions=listed(row["key_assumptions"]),
        open_questions=listed(row["open_questions"]),
        what_would_break_it=listed(row["what_would_break_it"]),
        source=row["source"],
        supersedes_id=row["supersedes_id"],
        llm_call_id=row["llm_call_id"],
        note=row["note"],
    )


def save_thesis(conn: sqlite3.Connection, thesis: Thesis) -> Thesis:
    """Record a new thesis version and supersede the previous one.

    Versions are per security and always increase. The previous active version
    is marked superseded rather than deleted, because the record of what was
    believed at the time is the reason the table exists.

    Args:
        conn: Open database connection.
        thesis: The thesis to record. Its ``version`` is assigned here.

    Returns:
        The stored thesis, with its id and version filled in.
    """
    with conn:
        row = conn.execute(
            "SELECT id, MAX(version) AS version FROM thesis WHERE security_id = ?",
            (thesis.security_id,),
        ).fetchone()
        previous_id = row["id"] if row and row["version"] is not None else None
        version = (row["version"] or 0) + 1 if row else 1

        conn.execute(
            "UPDATE thesis SET status = 'superseded' WHERE security_id = ? AND status = 'active'",
            (thesis.security_id,),
        )
        cursor = conn.execute(
            """
            INSERT INTO thesis (
                security_id, version, status, summary, rationale, conviction,
                thesis_status, key_assumptions, open_questions,
                what_would_break_it, source, supersedes_id, llm_call_id, note,
                created_at
            )
            VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                thesis.security_id,
                version,
                thesis.summary,
                thesis.rationale,
                thesis.conviction,
                thesis.thesis_status,
                json.dumps(list(thesis.key_assumptions)),
                json.dumps(list(thesis.open_questions)),
                json.dumps(list(thesis.what_would_break_it)),
                thesis.source,
                previous_id,
                thesis.llm_call_id,
                thesis.note,
                _now(),
            ),
        )
        stored_id = int(cursor.lastrowid)

    return _thesis_from_row(
        conn.execute("SELECT * FROM thesis WHERE id = ?", (stored_id,)).fetchone()
    )


def active_thesis(conn: sqlite3.Connection, *, security_id: int) -> Thesis | None:
    """Return the current thesis for one security, or None if it has none."""
    row = conn.execute(
        "SELECT * FROM thesis WHERE security_id = ? AND status = 'active'",
        (security_id,),
    ).fetchone()
    return _thesis_from_row(row) if row else None


def active_theses(conn: sqlite3.Connection) -> dict[int, Thesis]:
    """Return the current thesis for every security that has one."""
    return {
        int(row["security_id"]): _thesis_from_row(row)
        for row in conn.execute("SELECT * FROM thesis WHERE status = 'active'")
    }


def thesis_history(conn: sqlite3.Connection, *, security_id: int) -> list[Thesis]:
    """Return every version for one security, newest first."""
    return [
        _thesis_from_row(row)
        for row in conn.execute(
            "SELECT * FROM thesis WHERE security_id = ? ORDER BY version DESC",
            (security_id,),
        )
    ]


def create_research_run(
    conn: sqlite3.Connection,
    *,
    run_date: str,
    kind: str,
    trace_id: int | None = None,
    note: str | None = None,
) -> int:
    """Open a research run and return its id."""
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO research_run (run_date, kind, trace_id, note, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_date, kind, trace_id, note, _now()),
        )
    return int(cursor.lastrowid)
