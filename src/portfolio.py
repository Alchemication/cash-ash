"""Derive positions, valuations and concentration from the trade ledger.

Positions are computed rather than stored, so a corrected trade is immediately
reflected everywhere. Cost basis uses the average-cost method: the portfolio
holds fractional shares bought in small increments, where per-lot tracking adds
bookkeeping without changing any decision, and the broker reports a blended
figure anyway.

Public API:
    positions      -- net quantity and cost basis per security
    cash_eur       -- uninvested cash implied by the ledger
    holdings       -- positions valued at the best available price
    total_value    -- portfolio value including cash
    concentration  -- grouped weights by security, sector or theme

Example:
    from portfolio import holdings, concentration

    rows = holdings(conn, account_id=1)
    by_theme = concentration(rows, key="theme")
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict

from config import CONCENTRATION_ALERT_PCT, MAX_POSITION_WEIGHT_PCT
from models import ConcentrationRow, Holding, Position, Security
from store import (
    latest_prices,
    latest_snapshot,
    load_securities,
    load_trades,
)

logger = logging.getLogger(__name__)

# Quantities below this are treated as a fully closed position rather than a
# residue. Fractional-share brokers leave dust like 1e-9 after a full sell,
# which would otherwise show as an open holding worth EUR 0.00 forever.
_CLOSED_QUANTITY_EPSILON = 1e-9


def positions(
    conn: sqlite3.Connection, *, account_id: int | None = None
) -> list[Position]:
    """Return open positions derived from the trade ledger.

    Args:
        conn: Open database connection.
        account_id: Restrict to one account, or None for all.

    Returns:
        Open positions sorted by ticker. Closed positions are omitted.
    """
    securities = {security.id: security for security in load_securities(conn).values()}
    quantities: dict[int, float] = defaultdict(float)
    costs: dict[int, float] = defaultdict(float)
    realised: dict[int, float] = defaultdict(float)

    for trade in load_trades(conn, account_id=account_id):
        held = quantities[trade.security_id]
        if trade.side == "BUY":
            quantities[trade.security_id] = held + trade.quantity
            costs[trade.security_id] += trade.amount_eur + trade.fee_eur
            continue

        if held <= _CLOSED_QUANTITY_EPSILON:
            logger.warning(
                "Sell of %s units with no position (security_id=%s, %s); ignored",
                trade.quantity,
                trade.security_id,
                trade.trade_date,
            )
            continue

        # Average cost: a sell releases cost in proportion to the units sold,
        # capped at the whole position so an over-sell cannot leave negative
        # basis behind.
        sold = min(trade.quantity, held)
        released = costs[trade.security_id] * (sold / held)
        realised[trade.security_id] += trade.amount_eur - trade.fee_eur - released
        costs[trade.security_id] -= released
        quantities[trade.security_id] = held - sold

    result = [
        Position(
            security=securities[security_id],
            quantity=quantity,
            cost_basis_eur=costs[security_id],
            realised_pnl_eur=realised[security_id],
        )
        for security_id, quantity in quantities.items()
        if quantity > _CLOSED_QUANTITY_EPSILON
    ]
    return sorted(result, key=lambda position: position.security.ticker)


def cash_eur(conn: sqlite3.Connection, *, account_id: int | None = None) -> float:
    """Return uninvested cash implied by cash flows and trades.

    Read from ``v_cash_balance`` rather than recomputed here, so the chat
    agent's SQL and this function cannot disagree about what cash means. The
    view omits an account that has never moved money, hence the coalesce.

    Args:
        conn: Open database connection.
        account_id: Restrict to one account, or None for all.

    Returns:
        EUR cash balance. Contributions and sells add, buys and fees subtract.
    """
    sql = "SELECT COALESCE(SUM(cash_eur), 0.0) FROM v_cash_balance"
    params: tuple = ()
    if account_id is not None:
        sql += " WHERE account_id = ?"
        params = (account_id,)
    return float(conn.execute(sql, params).fetchone()[0])


def _unit_values_from_snapshot(
    conn: sqlite3.Connection, *, account_id: int
) -> tuple[dict[int, float], str | None, str | None]:
    """Return EUR value per unit implied by the latest snapshot.

    A snapshot records a total EUR value and a quantity, so dividing gives a
    per-unit price that still applies if the quantity has since changed. That
    is the only pricing available before a market-data feed exists, and the
    only pricing that will ever exist for manually priced instruments.

    Args:
        conn: Open database connection.
        account_id: Account whose snapshot to read.

    Returns:
        ``(unit values by security id, snapshot date, source)``.
    """
    found = latest_snapshot(conn, account_id=account_id)
    if found is None:
        return {}, None, None
    snapshot, rows = found
    unit_values = {
        security_id: row["value_eur"] / row["quantity"]
        for security_id, row in rows.items()
        if row["quantity"]
    }
    return unit_values, snapshot["snapshot_date"], snapshot["source"]


def _fx_to_eur(conn: sqlite3.Connection, currency: str) -> float | None:
    """Return the latest EUR-per-unit rate for *currency*, or None if unknown."""
    if currency == "EUR":
        return 1.0
    row = conn.execute(
        """
        SELECT rate FROM fx_rates
        WHERE base = ? AND quote = 'EUR'
        ORDER BY rate_date DESC LIMIT 1
        """,
        (currency,),
    ).fetchone()
    return float(row["rate"]) if row else None


def holdings(conn: sqlite3.Connection, *, account_id: int = 1) -> list[Holding]:
    """Return positions valued at the best price available.

    Pricing falls back in order: a stored market price converted at the stored
    FX rate, then the per-unit value implied by the latest snapshot, then None.
    A holding that cannot be priced reports ``value_eur = None`` rather than
    zero, so an unpriced instrument is visibly missing instead of silently
    shrinking the portfolio.

    Args:
        conn: Open database connection.
        account_id: Account to value.

    Returns:
        Valued holdings sorted by descending value, unpriced ones last.
    """
    prices = latest_prices(conn)
    snapshot_units, snapshot_date, snapshot_source = _unit_values_from_snapshot(
        conn, account_id=account_id
    )

    result: list[Holding] = []
    for position in positions(conn, account_id=account_id):
        security_id = position.security.id
        assert security_id is not None

        price_row = prices.get(security_id)
        if price_row is not None:
            rate = _fx_to_eur(conn, price_row["currency"])
            if rate is not None:
                result.append(
                    Holding(
                        position=position,
                        value_eur=position.quantity * price_row["close_native"] * rate,
                        price_date=price_row["price_date"],
                        price_source=price_row["source"],
                    )
                )
                continue
            logger.warning(
                "No %s/EUR rate stored; falling back to snapshot for %s",
                price_row["currency"],
                position.security.ticker,
            )

        unit_value = snapshot_units.get(security_id)
        if unit_value is not None:
            result.append(
                Holding(
                    position=position,
                    value_eur=position.quantity * unit_value,
                    price_date=snapshot_date,
                    price_source=snapshot_source,
                )
            )
            continue

        result.append(Holding(position=position, value_eur=None))

    return sorted(
        result,
        key=lambda holding: (holding.value_eur is None, -(holding.value_eur or 0)),
    )


def total_value(rows: list[Holding], *, cash: float) -> float:
    """Return portfolio value including cash, ignoring unpriced holdings.

    Args:
        rows: Valued holdings.
        cash: Uninvested EUR cash.

    Returns:
        EUR total. Unpriced holdings contribute nothing, so callers reporting a
        weight against this figure must also report how many were skipped.
    """
    return cash + sum(row.value_eur for row in rows if row.value_eur is not None)


def concentration(
    rows: list[Holding], *, key: str, total: float
) -> list[ConcentrationRow]:
    """Group holdings into weights by security, sector or theme.

    Theme weights may sum past 100%: a security carries several themes, and
    splitting its value between them would understate every one of them.

    Args:
        rows: Valued holdings.
        key: ``security``, ``sector`` or ``theme``.
        total: Portfolio total to weigh against, cash included.

    Returns:
        Groups sorted by descending weight.

    Raises:
        ValueError: If *key* is not a supported grouping.
    """
    if key not in {"security", "sector", "theme"}:
        raise ValueError(
            f"Unknown grouping {key!r}. Use 'security', 'sector' or 'theme'."
        )

    limit = MAX_POSITION_WEIGHT_PCT if key == "security" else CONCENTRATION_ALERT_PCT
    grouped: dict[str, list[tuple[str, float]]] = defaultdict(list)

    for row in rows:
        if row.value_eur is None:
            continue
        security = row.position.security
        for label in _labels(security, key):
            grouped[label].append((security.ticker, row.value_eur))

    result = [
        ConcentrationRow(
            label=label,
            value_eur=sum(value for _, value in members),
            weight_pct=(sum(value for _, value in members) / total * 100)
            if total
            else 0.0,
            members=tuple(sorted(ticker for ticker, _ in members)),
            over_limit=(sum(value for _, value in members) / total * 100) > limit
            if total
            else False,
        )
        for label, members in grouped.items()
    ]
    return sorted(result, key=lambda group: -group.weight_pct)


def _labels(security: Security, key: str) -> tuple[str, ...]:
    """Return the grouping labels a security contributes to."""
    if key == "security":
        return (security.ticker,)
    if key == "sector":
        return (security.sector or "Unclassified",)
    return security.themes or ("untagged",)
