"""Deterministic valuation freshness shared by reports and trade constraints."""

from __future__ import annotations

import sqlite3
from datetime import date

from config import PRICE_STALE_AFTER_DAYS
from models import Holding


def valuation_gaps(
    conn: sqlite3.Connection, rows: list[Holding], today: date
) -> list[str]:
    """Name missing or stale prices and FX rates without treating them as zero."""
    gaps: list[str] = []
    for row in rows:
        ticker = row.position.security.ticker
        if row.value_eur is None:
            gaps.append(f"{ticker}: unpriced")
            continue
        if (
            not row.price_date
            or not 0
            <= (today - date.fromisoformat(row.price_date)).days
            <= PRICE_STALE_AFTER_DAYS
        ):
            gaps.append(
                f"{ticker}: price stale or undated ({row.price_date or 'unknown'})"
            )
        currency = row.position.security.currency
        if currency != "EUR":
            fx = conn.execute(
                "SELECT rate_date FROM fx_rates WHERE base=? AND quote='EUR' ORDER BY rate_date DESC LIMIT 1",
                (currency,),
            ).fetchone()
            if (
                fx is None
                or not 0
                <= (today - date.fromisoformat(fx["rate_date"])).days
                <= PRICE_STALE_AFTER_DAYS
            ):
                gaps.append(f"{ticker}: {currency}/EUR FX missing or stale")
    return gaps


def security_price_gap(
    conn: sqlite3.Connection, security_id: int, today: date
) -> str | None:
    """Require a current quote and FX for a proposed purchase of a new security."""
    row = conn.execute(
        """SELECT p.*,s.ticker FROM prices p JOIN securities s ON s.id=p.security_id
        WHERE p.security_id=? ORDER BY p.price_date DESC LIMIT 1""",
        (security_id,),
    ).fetchone()
    if row is None:
        return "No quote for this security. Sync or enter a price before recommending a purchase."
    if (
        not 0
        <= (today - date.fromisoformat(row["price_date"])).days
        <= PRICE_STALE_AFTER_DAYS
    ):
        return "Quote is stale. Sync before recommending a purchase."
    if row["currency"] != "EUR":
        fx = conn.execute(
            "SELECT rate_date FROM fx_rates WHERE base=? AND quote='EUR' ORDER BY rate_date DESC LIMIT 1",
            (row["currency"],),
        ).fetchone()
        if (
            fx is None
            or not 0
            <= (today - date.fromisoformat(fx["rate_date"])).days
            <= PRICE_STALE_AFTER_DAYS
        ):
            return "FX is missing or stale. Sync before recommending a purchase."
    return None
