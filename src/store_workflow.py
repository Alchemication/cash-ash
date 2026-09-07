"""Persistence for review progress, owner responses and research assessments."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime


def now_iso() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(UTC).isoformat()


def latest_assessments(conn: sqlite3.Connection) -> list[dict]:
    """Load the latest completed assessment of each security."""
    return [
        dict(row)
        for row in conn.execute(
            """SELECT a.*, r.run_date, s.ticker FROM research_assessment a
        JOIN research_run r ON r.id = a.run_id
        JOIN securities s ON s.id = a.security_id
        WHERE a.run_id = (SELECT MAX(b.run_id) FROM research_assessment b
                          WHERE b.security_id = a.security_id)"""
        )
    ]


def start_cycle(conn: sqlite3.Connection, run_date: str) -> int:
    """Record a running cycle before any network work starts."""
    with conn:
        cursor = conn.execute(
            "INSERT INTO review_cycle(run_date,status,started_at) VALUES (?,'running',?)",
            (run_date, now_iso()),
        )
    return int(cursor.lastrowid)


def finish_cycle(conn: sqlite3.Connection, cycle_id: int, stages: list[dict]) -> None:
    """Persist all stages, including partial failures and deliberate skips."""
    complete = all(stage["ok"] for stage in stages)
    with conn:
        conn.execute(
            "UPDATE review_cycle SET status=?,stages_json=?,finished_at=? WHERE id=?",
            (
                "complete" if complete else "incomplete",
                json.dumps(stages),
                now_iso(),
                cycle_id,
            ),
        )


def latest_cycle(conn: sqlite3.Connection) -> dict | None:
    """Load the latest review health, or None before the first cycle."""
    row = conn.execute("SELECT * FROM review_cycle ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def latest_response(conn: sqlite3.Connection, recommendation_id: int) -> dict | None:
    """Return the current response, preserving earlier decisions in history."""
    row = conn.execute(
        "SELECT * FROM user_decision WHERE recommendation_id=? ORDER BY id DESC LIMIT 1",
        (recommendation_id,),
    ).fetchone()
    return dict(row) if row else None


def write_response(
    conn: sqlite3.Connection,
    recommendation_id: int,
    decision: str,
    note: str | None,
    snoozed_until: str | None,
) -> None:
    """Append a decision and its optional snooze deadline."""
    with conn:
        conn.execute(
            """INSERT INTO user_decision(recommendation_id,decision,note,decided_at,snoozed_until)
               VALUES (?,?,?,?,?)""",
            (recommendation_id, decision, note, now_iso(), snoozed_until),
        )


def review_thesis(
    conn: sqlite3.Connection, security_id: int, version: int, accept: bool
) -> None:
    """Atomically accept or reject a proposal based on the still-active thesis."""
    with conn:
        row = conn.execute(
            "SELECT * FROM thesis WHERE security_id=? AND version=?",
            (security_id, version),
        ).fetchone()
        if row is None or row["status"] != "proposed":
            raise ValueError(
                "Choose a proposed version from main.py thesis show TICKER."
            )
        active = conn.execute(
            "SELECT id FROM thesis WHERE security_id=? AND status='active'",
            (security_id,),
        ).fetchone()
        if accept and (active is None or active["id"] != row["supersedes_id"]):
            raise ValueError(
                "This proposal uses an older thesis. Run research again before accepting."
            )
        if accept:
            conn.execute(
                "UPDATE thesis SET status='superseded' WHERE security_id=? AND status IN ('active','proposed')",
                (security_id,),
            )
        conn.execute(
            "UPDATE thesis SET status=?,note=? WHERE id=?",
            (
                "active" if accept else "rejected",
                f"{'Accepted' if accept else 'Rejected'} by owner at {now_iso()}",
                row["id"],
            ),
        )


def reservation_rows(conn: sqlite3.Connection) -> list[dict]:
    """Load outstanding buys and current-week executions for domain budgeting."""
    return [
        dict(row)
        for row in conn.execute("""
        SELECT r.*, d.decision, e.id AS execution_id, e.outcome,
               e.amount_eur AS executed_amount, e.executed_on
        FROM recommendation r
        LEFT JOIN user_decision d ON d.id=(SELECT MAX(x.id) FROM user_decision x
                                         WHERE x.recommendation_id=r.id)
        LEFT JOIN execution e ON e.recommendation_id=r.id
        WHERE r.action IN ('BUY','ADD')
    """)
    ]


def save_execution(
    conn: sqlite3.Connection,
    *,
    recommendation_id: int,
    security_id: int,
    side: str,
    quantity: float,
    amount: float,
    fee: float,
    day: str,
    note: str | None,
    skipped: bool,
) -> None:
    """Write a trade and its execution link in one transaction."""
    with conn:
        trade_id = None
        if not skipped:
            cursor = conn.execute(
                """INSERT INTO trades
                (account_id,security_id,trade_date,side,quantity,amount_eur,fee_eur,is_synthetic,note,created_at)
                VALUES (1,?,?,?,?,?,?,0,?,?)""",
                (security_id, day, side, quantity, amount, fee, note, now_iso()),
            )
            trade_id = cursor.lastrowid
        conn.execute(
            """INSERT INTO execution
            (recommendation_id,trade_id,executed_on,quantity,amount_eur,note,created_at,outcome)
            VALUES (?,?,?,?,?,?,?,?)""",
            (
                recommendation_id,
                trade_id,
                day,
                None if skipped else quantity,
                None if skipped else amount,
                note,
                now_iso(),
                "not_executed" if skipped else "executed",
            ),
        )
