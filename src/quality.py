"""Deterministic valuation freshness shared by reports and trade constraints."""

from __future__ import annotations

import sqlite3
from datetime import date

from config import PRICE_STALE_AFTER_DAYS
from models import Holding


def _is_current(stamp: str | None, today: date) -> bool:
    """Return whether a date is recent enough to value against."""
    if not stamp:
        return False
    return 0 <= (today - date.fromisoformat(stamp)).days <= PRICE_STALE_AFTER_DAYS


def _fx_date(conn: sqlite3.Connection, currency: str) -> str | None:
    """Return the date of the latest stored rate for *currency*, or None."""
    row = conn.execute(
        "SELECT rate_date FROM fx_rates WHERE base=? AND quote='EUR' "
        "ORDER BY rate_date DESC LIMIT 1",
        (currency,),
    ).fetchone()
    return row["rate_date"] if row else None


def valuation_gaps(
    conn: sqlite3.Connection, rows: list[Holding], today: date
) -> list[str]:
    """Name missing or stale prices and FX rates without treating them as zero.

    One entry per holding per problem, which is what a caller inspecting a
    single security wants. For anything rendered to a person, use
    :func:`valuation_summary` instead — a portfolio whose sync is a week behind
    produces one of these for every holding twice over, and a wall of identical
    clauses is read as noise rather than as the one fact behind them.
    """
    gaps: list[str] = []
    for row in rows:
        ticker = row.position.security.ticker
        if row.value_eur is None:
            gaps.append(f"{ticker}: unpriced")
            continue
        if not _is_current(row.price_date, today):
            gaps.append(
                f"{ticker}: price stale or undated ({row.price_date or 'unknown'})"
            )
        currency = row.position.security.currency
        if currency != "EUR" and not _is_current(_fx_date(conn, currency), today):
            gaps.append(f"{ticker}: {currency}/EUR FX missing or stale")
    return gaps


def valuation_summary(
    conn: sqlite3.Connection, rows: list[Holding], today: date
) -> list[str]:
    """Return the same problems grouped by cause, for reading.

    A sync that has not run for a week is one fact, not twenty-eight. Grouped
    by what is actually wrong — these holdings cannot be priced, prices are this
    old, this currency's rate is this old — with the remedy stated once.

    Args:
        conn: Open database connection.
        rows: Valued holdings.
        today: Reference date.

    Returns:
        A line per distinct problem, empty when the valuation is sound.
    """
    unpriced = [row.position.security.ticker for row in rows if row.value_eur is None]
    stale_dates = sorted(
        {
            row.price_date or "unknown"
            for row in rows
            if row.value_eur is not None and not _is_current(row.price_date, today)
        }
    )
    stale_count = sum(
        1
        for row in rows
        if row.value_eur is not None and not _is_current(row.price_date, today)
    )
    currencies = sorted(
        {
            row.position.security.currency
            for row in rows
            if row.position.security.currency != "EUR"
        }
    )
    stale_fx = {
        currency: _fx_date(conn, currency)
        for currency in currencies
        if not _is_current(_fx_date(conn, currency), today)
    }

    lines: list[str] = []
    if unpriced:
        lines.append(
            f"Cannot be priced: {', '.join(unpriced)}. Excluded from the total "
            f"and from every weight."
        )
    if stale_count:
        oldest = stale_dates[0] if stale_dates else "unknown"
        age = (
            f", {(today - date.fromisoformat(oldest)).days} days old"
            if oldest != "unknown"
            else ""
        )
        scope = (
            "Every price is stale"
            if stale_count == len([row for row in rows if row.value_eur is not None])
            else f"{stale_count} prices are stale"
        )
        lines.append(f"{scope} (oldest {oldest}{age}).")
    for currency, stamp in stale_fx.items():
        lines.append(
            f"{currency}/EUR rate is {'missing' if stamp is None else f'from {stamp}'}."
        )
    if stale_count or stale_fx:
        lines.append("Run 'main.py sync' to refresh them.")
    return lines


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
