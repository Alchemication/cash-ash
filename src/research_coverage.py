"""Reserve research capacity for holdings that have not been checked recently."""

from __future__ import annotations

import sqlite3
from datetime import date

from config import (
    RESEARCH_ASSET_CLASSES,
    RESEARCH_OVERDUE_DAYS,
    RESEARCH_ROTATION_SLOTS,
)
from portfolio import positions
from store_research import active_theses
from store_workflow import latest_assessments


def select_research(
    conn: sqlite3.Connection,
    selected: list[tuple[str, str]],
    *,
    today: date,
    limit: int,
) -> tuple[list[tuple[str, str]], list[str]]:
    """Preserve event priorities while reserving slots for oldest completed coverage."""
    if limit < 0:
        raise ValueError("Research cap must be nonnegative.")
    holdings = positions(conn, account_id=1)
    theses = active_theses(conn)
    dates = {a["security_id"]: a["run_date"] for a in latest_assessments(conn)}
    eligible = {
        p.security.ticker
        for p in holdings
        if p.security.asset_class in RESEARCH_ASSET_CLASSES and p.security.id in theses
    }
    overdue = sorted(
        [
            (dates.get(p.security.id, "0001-01-01"), p.security.ticker)
            for p in holdings
            if p.security.ticker in eligible
            and (
                today - date.fromisoformat(dates.get(p.security.id, "0001-01-01"))
            ).days
            >= RESEARCH_OVERDUE_DAYS
        ]
    )
    rotation = [
        (ticker, "Coverage overdue; check thesis assumptions")
        for _, ticker in overdue[: min(limit, RESEARCH_ROTATION_SLOTS)]
    ]
    reserved = {t for t, _ in rotation}
    priority = [(t, r) for t, r in selected if t in eligible and t not in reserved]
    targets = priority[: max(0, limit - len(rotation))] + rotation
    chosen = {t for t, _ in targets}
    for _, ticker in overdue:
        if len(targets) >= limit:
            break
        if ticker not in chosen:
            targets.append((ticker, "Coverage overdue; use spare research capacity"))
            chosen.add(ticker)
    manual = [p.security.ticker for p in holdings if p.security.ticker not in eligible]
    deferred = list(dict.fromkeys([t for t, _ in selected if t not in chosen] + manual))
    return targets, deferred
