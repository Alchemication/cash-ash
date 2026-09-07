"""Owner decisions, funded commitments and manual execution validation."""

from __future__ import annotations

import math
import sqlite3
from datetime import date, timedelta

from config import SNOOZE_DAYS
from store_workflow import (
    latest_response,
    reservation_rows,
    save_execution,
    write_response,
)


def capital_committed(
    conn: sqlite3.Connection, today: date
) -> tuple[float, float, dict[str, float]]:
    """Return reserved cash, weekly deployed/committed money, and reserved positions."""
    monday = today - timedelta(days=today.weekday())
    reserved = weekly = 0.0
    by_ticker: dict[str, float] = {}
    securities = {
        r["id"]: r["ticker"] for r in conn.execute("SELECT id,ticker FROM securities")
    }
    for row in reservation_rows(conn):
        if row["execution_id"] is not None:
            if (
                row["outcome"] == "executed"
                and monday.isoformat() <= row["executed_on"] <= today.isoformat()
            ):
                weekly += row["executed_amount"] or 0
            continue
        # Approved intent stays reserved until execution or an explicit rejection,
        # even after the original recommendation expires.
        if row["decision"] != "approve":
            continue
        amount = row["amount_eur"] or 0
        reserved += amount
        weekly += amount
        ticker = securities.get(row["security_id"])
        if ticker:
            by_ticker[ticker] = by_ticker.get(ticker, 0) + amount
    return reserved, weekly, by_ticker


def record_response(
    conn: sqlite3.Connection,
    recommendation_id: int,
    decision: str,
    *,
    note: str | None = None,
    today: date | None = None,
) -> str:
    """Serialize validation and reservation so two approvals cannot spend the same cash."""
    with conn:
        conn.execute("UPDATE recommendation SET id=id WHERE id=?", (recommendation_id,))
        return _record_response(
            conn, recommendation_id, decision, note=note, today=today
        )


def _record_response(
    conn: sqlite3.Connection,
    recommendation_id: int,
    decision: str,
    *,
    note: str | None = None,
    today: date | None = None,
) -> str:
    """Validate a response; approval rechecks funding and market-data quality."""
    from decisions import build_guardrail_context
    from guardrails import check_proposal

    now = today or date.today()
    if decision not in {"approve", "reject", "later"}:
        raise ValueError("Choose approve, reject or later.")
    row = conn.execute(
        "SELECT r.*,s.ticker FROM recommendation r LEFT JOIN securities s ON s.id=r.security_id WHERE r.id=?",
        (recommendation_id,),
    ).fetchone()
    if row is None:
        raise ValueError("No such recommendation. Run main.py decide to list them.")
    if conn.execute(
        "SELECT 1 FROM execution WHERE recommendation_id=?", (recommendation_id,)
    ).fetchone():
        raise ValueError("Execution already recorded; this decision is closed.")
    previous = latest_response(conn, recommendation_id)
    if previous and previous["decision"] == decision and decision != "later":
        return f"Already recorded: {decision}."
    if not (decision == "reject" and previous and previous["decision"] == "approve"):
        if row["superseded_by_run_id"] is not None:
            raise ValueError(
                "This recommendation was replaced by a later run. Run main.py decide."
            )
        if row["expires_on"] < now.isoformat():
            raise ValueError(
                f"Expired on {row['expires_on']}. Run main.py recommend for a fresh review."
            )
    if previous and previous["decision"] == "approve" and decision == "later":
        raise ValueError(
            "Approved actions reserve cash. Reject to cancel, or record execution before snoozing another proposal."
        )
    if decision == "approve" and row["action"] in {"BUY", "ADD", "TRIM", "EXIT"}:
        from store_workflow import latest_cycle

        cycle = latest_cycle(conn)
        if (
            cycle
            and cycle["status"] != "complete"
            and cycle["run_date"] >= row["run_date"]
        ):
            raise ValueError(
                "Latest weekly review is incomplete. Complete main.py weekly before approving a trade."
            )
        if row["action"] == "BUY":
            from quality import security_price_gap

            gap = security_price_gap(conn, row["security_id"], now)
            if gap:
                raise ValueError(gap)
        context = build_guardrail_context(conn, today=now)
        verdict = check_proposal(
            action=row["action"],
            ticker=row["ticker"],
            amount_eur=row["amount_eur"],
            context=context,
        )
        if (
            verdict.refused
            or verdict.action != row["action"]
            or verdict.amount_eur != row["amount_eur"]
        ):
            raise ValueError(
                "Portfolio or funding changed. Run main.py recommend before approving this trade."
            )
    until = (
        min(now + timedelta(days=SNOOZE_DAYS), date.fromisoformat(row["expires_on"]))
        if decision == "later"
        else None
    )
    write_response(
        conn, recommendation_id, decision, note, until.isoformat() if until else None
    )
    if until:
        return f"Snoozed until {until.isoformat()}; it will return in /pending."
    return (
        "Recorded. Nothing has been traded — execute it yourself."
        if decision == "approve"
        else "Recorded: reject."
    )


def record_execution(
    conn: sqlite3.Connection,
    recommendation_id: int,
    *,
    quantity: float = 0,
    amount: float = 0,
    fee: float = 0,
    day: str | None = None,
    note: str | None = None,
    skipped: bool = False,
) -> None:
    """Serialize fill validation and ledger insertion against other recorded fills."""
    with conn:
        conn.execute("UPDATE recommendation SET id=id WHERE id=?", (recommendation_id,))
        _record_execution(
            conn,
            recommendation_id,
            quantity=quantity,
            amount=amount,
            fee=fee,
            day=day,
            note=note,
            skipped=skipped,
        )


def _record_execution(
    conn: sqlite3.Connection,
    recommendation_id: int,
    *,
    quantity: float = 0,
    amount: float = 0,
    fee: float = 0,
    day: str | None = None,
    note: str | None = None,
    skipped: bool = False,
) -> None:
    """Record an actual fill, checking finite amounts, chronology and oversells."""
    from portfolio import positions

    row = conn.execute(
        "SELECT * FROM recommendation WHERE id=?", (recommendation_id,)
    ).fetchone()
    if (
        row is None
        or row["action"] not in {"BUY", "ADD", "TRIM", "EXIT"}
        or row["security_id"] is None
    ):
        raise ValueError("Choose a trade recommendation from main.py decide.")
    response = latest_response(conn, recommendation_id)
    if response is None or response["decision"] != "approve":
        raise ValueError("Record approval with main.py decide ID approve first.")
    if conn.execute(
        "SELECT 1 FROM execution WHERE recommendation_id=?", (recommendation_id,)
    ).fetchone():
        raise ValueError(
            "Execution already recorded. Inspect the ledger before making a correction."
        )
    executed = date.fromisoformat(day) if day else date.today()
    if executed > date.today() or executed.isoformat() < row["run_date"]:
        raise ValueError(
            "Execution date must be between the recommendation date and today."
        )
    if not skipped:
        if (
            not all(math.isfinite(x) for x in (quantity, amount, fee))
            or quantity <= 0
            or amount <= 0
            or fee < 0
        ):
            raise ValueError(
                "Enter positive quantity and EUR amount, and a nonnegative fee."
            )
        latest = conn.execute(
            "SELECT MAX(trade_date) FROM trades WHERE security_id=?",
            (row["security_id"],),
        ).fetchone()[0]
        if latest and executed.isoformat() < latest:
            raise ValueError(
                "A later trade already exists; reconcile the ledger before inserting this fill."
            )
        if row["action"] in {"TRIM", "EXIT"}:
            held = next(
                (
                    p.quantity
                    for p in positions(conn, account_id=1)
                    if p.security.id == row["security_id"]
                ),
                0,
            )
            if quantity > held:
                raise ValueError(
                    "Quantity exceeds the recorded holding. Reconcile the ledger first."
                )
    save_execution(
        conn,
        recommendation_id=recommendation_id,
        security_id=row["security_id"],
        side="BUY" if row["action"] in {"BUY", "ADD"} else "SELL",
        quantity=quantity,
        amount=amount,
        fee=fee,
        day=executed.isoformat(),
        note=note,
        skipped=skipped,
    )
