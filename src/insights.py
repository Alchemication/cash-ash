"""Portfolio facts for the weekly summary, computed rather than written.

Every line comes from the ledger, stored prices, the owner's theses and the
configured limits. No model is involved, and that is the point: these are the
facts any recommendation has to be consistent with, and each can be checked
against ``main.py concentration`` or ``main.py holdings``. A line that would
need a judgement belongs somewhere else.

Public API:
    portfolio_picture -- the handful of facts that describe the portfolio now

Example:
    from insights import portfolio_picture

    for line in portfolio_picture(conn, today=date.today()):
        print(line)
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from config import (
    CONCENTRATION_ALERT_PCT,
    MAX_POSITION_WEIGHT_PCT,
    REPORT_COMPARISON_DAYS,
)
from buy_sell_rules import DEFENSIBLE_CONVICTION
from portfolio import Holding, cash_eur, concentration, holdings, total_value


_NEW_MONEY_KINDS = frozenset({"OPENING_BALANCE", "CONTRIBUTION", "WITHDRAWAL"})
"""Cash flows that move money across the account boundary.

A deposit raises the total without anything having gone up, so a change in
value is only a gain once these are taken out. Dividends and fees stay in: they
are the portfolio's own doing.
"""


def portfolio_picture(
    conn: sqlite3.Connection, *, account_id: int = 1, today: date | None = None
) -> list[str]:
    """Return the facts that describe the portfolio this week, in reading order.

    Args:
        conn: Open database connection.
        account_id: Account to describe.
        today: Reference date, for tests.

    Returns:
        Plain-text lines, not yet escaped. Empty when nothing held is priced.
    """
    now = today or date.today()
    rows = holdings(conn, account_id=account_id)
    priced = [row for row in rows if row.value_eur is not None]
    cash = cash_eur(conn, account_id=account_id)
    total = total_value(rows, cash=cash)
    if not priced or total <= 0:
        return []

    lines: list[str] = []
    if len(priced) < len(rows):
        lines.append(
            f"Weights leave out {len(rows) - len(priced)} holding(s) that could "
            f"not be priced."
        )
    lines.append(_largest(priced, total))
    group = _heaviest_group(priced, total)
    if group:
        lines.append(group)
    lines.append(_currency(priced))
    lines.append(f"Cash {_money(cash)}, {cash / total * 100:.0f}% of the portfolio.")
    lines.append(_reasons(conn, rows, now))
    change = _since(
        conn, account_id=account_id, total=total, now=now, complete=priced == rows
    )
    if change:
        lines.append(change)
    benchmark = _benchmark(conn, account_id=account_id, now=now)
    if benchmark:
        lines.append(benchmark)
    return lines


def _money(value: float) -> str:
    """Format a EUR amount for a phone screen."""
    return f"€{value:,.2f}"


def _day(value: date) -> str:
    """Format a date as a short day, e.g. ``Sun 13 Sep``."""
    return f"{value:%a} {value.day} {value:%b}"


def _largest(priced: list[Holding], total: float) -> str:
    """The largest position against the position limit."""
    top = concentration(priced, key="security", total=total)[0]
    if top.over_limit:
        return (
            f"Largest: {top.label} at {top.weight_pct:.1f}%, over the "
            f"{MAX_POSITION_WEIGHT_PCT:.0f}% position limit."
        )
    return (
        f"Largest: {top.label} at {top.weight_pct:.1f}%, under the "
        f"{MAX_POSITION_WEIGHT_PCT:.0f}% position limit."
    )


def _heaviest_group(priced: list[Holding], total: float) -> str | None:
    """The heaviest theme against the concentration alert level.

    Themes rather than sectors: sector labels split one bet across several
    names — large US technology sits in three GICS sectors — and hide exactly
    the concentration worth seeing.
    """
    groups = [
        group
        for group in concentration(priced, key="theme", total=total)
        if group.label != "untagged"
    ]
    if not groups:
        return None
    top = groups[0]
    name = top.label.replace("-", " ")
    count = len(top.members)
    holdings_word = "holding" if count == 1 else "holdings"
    if top.over_limit:
        return (
            f"Heaviest theme: {name}, {top.weight_pct:.1f}% across {count} "
            f"{holdings_word}, over the {CONCENTRATION_ALERT_PCT:.0f}% alert level. "
            f"Adding to any of them makes it heavier."
        )
    return (
        f"Heaviest theme: {name}, {top.weight_pct:.1f}% across {count} "
        f"{holdings_word}, under the {CONCENTRATION_ALERT_PCT:.0f}% alert level."
    )


def _currency(priced: list[Holding]) -> str:
    """Which currencies the holdings are priced in.

    Money is held in EUR, so any other currency is a second bet riding on every
    return: a stock can rise while its euro value falls.
    """
    by_currency: dict[str, float] = {}
    for row in priced:
        currency = row.position.security.currency
        by_currency[currency] = by_currency.get(currency, 0.0) + (row.value_eur or 0.0)
    if len(by_currency) == 1:
        (currency,) = by_currency
        if currency == "EUR":
            return "Every holding is priced in EUR, so exchange rates do not move it."
        return (
            f"Every holding is priced in {currency}: part of each return is the "
            f"{currency} against the euro."
        )
    invested = sum(by_currency.values()) or 1.0
    parts = ", ".join(
        f"{currency} {value / invested * 100:.0f}%"
        for currency, value in sorted(by_currency.items(), key=lambda item: -item[1])
    )
    return f"Priced in {parts}. Anything not in EUR also moves with its exchange rate."


def _reasons(conn: sqlite3.Connection, rows: list[Holding], now: date) -> str:
    """How many holdings have a reason the owner could defend, and the trend."""
    held = {row.position.security.id for row in rows}
    current = _defensible_count(conn, held, as_of=None)
    line = f"Reasons you could defend: {current or 0} of {len(held)}"
    cutoff = now - timedelta(days=REPORT_COMPARISON_DAYS)
    then = _defensible_count(conn, held, as_of=cutoff)
    if then is None or current is None:
        return line + "."
    if then == current:
        return line + f", unchanged since {_day(cutoff)}."
    direction = "up" if current > then else "down"
    return line + f", {direction} from {then} on {_day(cutoff)}."


def _defensible_count(
    conn: sqlite3.Connection, held: set[int | None], *, as_of: date | None
) -> int | None:
    """Count held securities whose thesis was moderate or better at a date.

    A past date reads the latest non-proposed version created by then. An
    accepted proposal counts from when it was proposed rather than accepted,
    which can move a change a few days earlier; the direction is still right.

    Returns:
        The count, or None when no thesis existed at that date to compare with.
    """
    if as_of is None:
        found = conn.execute(
            "SELECT security_id, conviction FROM thesis WHERE status = 'active'"
        ).fetchall()
    else:
        found = conn.execute(
            """
            SELECT t.security_id, t.conviction FROM thesis t
            WHERE t.status IN ('active', 'superseded')
              AND substr(t.created_at, 1, 10) <= :day
              AND t.version = (
                  SELECT MAX(x.version) FROM thesis x
                  WHERE x.security_id = t.security_id
                    AND x.status IN ('active', 'superseded')
                    AND substr(x.created_at, 1, 10) <= :day
              )
            """,
            {"day": as_of.isoformat()},
        ).fetchall()
    if not found:
        return None
    return sum(1 for row in found if row[0] in held and row[1] in DEFENSIBLE_CONVICTION)


def _since(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    total: float,
    now: date,
    complete: bool,
) -> str | None:
    """Change in value since the last snapshot a comparison period ago.

    Money added or withdrawn in between is taken out, since a deposit is not a
    gain. Skipped when a holding is unpriced now, and snapshots that omitted
    an unpriced holding are passed over: either side missing a holding would
    show its value as a loss or a gain that never happened.
    """
    from store import load_cash_flows

    if not complete:
        return None
    cutoff = (now - timedelta(days=REPORT_COMPARISON_DAYS)).isoformat()
    snapshot = conn.execute(
        """
        SELECT snapshot_date, total_value_eur FROM portfolio_snapshots
        WHERE account_id = ? AND snapshot_date <= ?
          AND COALESCE(note, '') NOT LIKE '%omitted%'
        ORDER BY snapshot_date DESC, id DESC LIMIT 1
        """,
        (account_id, cutoff),
    ).fetchone()
    if snapshot is None:
        return None
    since = snapshot[0]
    moved = sum(
        flow.amount_eur
        for flow in load_cash_flows(conn, account_id=account_id)
        if flow.kind in _NEW_MONEY_KINDS and since < flow.flow_date <= now.isoformat()
    )
    change = total - snapshot[1] - moved
    line = (
        f"{'Up' if change >= 0 else 'Down'} {_money(abs(change))} since "
        f"{_day(date.fromisoformat(since))}"
    )
    if moved > 0:
        line += f", not counting {_money(moved)} added"
    elif moved < 0:
        line += f", not counting {_money(-moved)} withdrawn"
    return line + "."


def _benchmark(conn: sqlite3.Connection, *, account_id: int, now: date) -> str | None:
    """The same money in the passive benchmark, with its caveat while young."""
    from benchmark import compare

    try:
        comparison = compare(conn, account_id=account_id, today=now)
    except ValueError:
        return None
    from config import BENCHMARK_MEANINGFUL_AFTER_DAYS

    if comparison.invested_eur <= 0:
        return None
    # Two euro figures side by side read as a score, and a few months of
    # difference is noise. Until the comparison means something it says only
    # that it is recording, as the benchmark's own verdict does.
    if not comparison.is_meaningful:
        return (
            f"Benchmark ({comparison.name}): recording, {comparison.days} of "
            f"{BENCHMARK_MEANINGFUL_AFTER_DAYS:,} days before it means anything."
        )
    return (
        f"Same money in {comparison.name}: {_money(comparison.benchmark_eur)}; "
        f"yours: {_money(comparison.portfolio_eur)}."
    )
