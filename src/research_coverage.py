"""Reserve research capacity for holdings that have not been checked recently."""

from __future__ import annotations

import sqlite3
from datetime import date

from config import (
    NEVER_RESEARCHED,
    RESEARCH_ASSET_CLASSES,
    RESEARCH_OVERDUE_DAYS,
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
    """Fill the week's slots from triage first, then with the stalest coverage.

    Triage ranks the whole portfolio against what actually happened, so its
    picks take the slots and rotation gets what is left. Reserving a slot for
    the backlog instead inverted that: on 2026-09-20 a holding triage ranked
    twelfth of fourteen, on a 0.5% estimate change it called noise, displaced
    the two it had selected for having no defensible reason at all.

    Rotation still cannot starve, because a quiet week leaves slots spare and
    the backlog sorts oldest first.

    Args:
        conn: Open database connection.
        selected: Triage's picks as ``(ticker, reason)``, best first.
        today: Reference date.
        limit: Maximum deep passes this week.

    Returns:
        ``(targets, deferred)`` — what to research as ``(ticker, reason)``,
        and the tickers left for a later week or for manual review.
    """
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
            (
                dates.get(p.security.id, NEVER_RESEARCHED),
                # Cost basis, not market value: it is what the owner actually
                # committed, it needs no price feed, and an unpriceable
                # holding must not sort as if it were worth nothing.
                -p.cost_basis_eur,
                p.security.ticker,
            )
            for p in holdings
            if p.security.ticker in eligible
            and (
                today - date.fromisoformat(dates.get(p.security.id, NEVER_RESEARCHED))
            ).days
            >= RESEARCH_OVERDUE_DAYS
        ]
    )
    targets = [(t, r) for t, r in selected if t in eligible][:limit]
    chosen = {t for t, _ in targets}
    for _, _, ticker in overdue:
        if len(targets) >= limit:
            break
        if ticker not in chosen:
            targets.append((ticker, "Coverage overdue; use spare research capacity"))
            chosen.add(ticker)
    manual = [p.security.ticker for p in holdings if p.security.ticker not in eligible]
    deferred = list(dict.fromkeys([t for t, _ in selected if t not in chosen] + manual))
    return targets, deferred
