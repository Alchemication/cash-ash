"""Comparing the portfolio against doing nothing clever.

The comparison is money-weighted, not a return-versus-return figure. Money
arrives over time, so "the portfolio is up 3% and the index is up 5%" answers a
question nobody asked: it silently assumes every euro was present from the
start. What is wanted is what the same money, moved on the same days, would be
worth in a tracker instead.

That is the only measure of this system likely to mean anything. At fourteen
holdings a week nothing else has the sample size to separate skill from noise,
and forward-tracking it is the one thing that cannot be reconstructed later.

Public API:
    ensure_benchmark   -- register the benchmark security
    sync_benchmark     -- fetch its history from the first cash flow onwards
    compare            -- what the same money would be worth passively
    Comparison         -- the result

Example:
    from benchmark import compare

    result = compare(conn)
    print(result.difference_eur)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from config import BENCHMARK_MEANINGFUL_AFTER_DAYS, BENCHMARK_NAME, BENCHMARK_TICKER

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Comparison:
    """The portfolio beside its passive alternative."""

    ticker: str
    name: str
    invested_eur: float
    portfolio_eur: float
    benchmark_eur: float
    units: float
    priced_on: str | None
    since: str | None = None
    unpriced_flows: tuple[str, ...] = ()

    @property
    def days(self) -> int:
        """How long the comparison has been running."""
        if not (self.since and self.priced_on):
            return 0
        return (
            date.fromisoformat(self.priced_on) - date.fromisoformat(self.since)
        ).days

    @property
    def difference_eur(self) -> float:
        """How much better or worse the portfolio has done, in euro."""
        return self.portfolio_eur - self.benchmark_eur

    @property
    def portfolio_return_pct(self) -> float | None:
        """Money-weighted return of the portfolio."""
        if self.invested_eur <= 0:
            return None
        return (self.portfolio_eur / self.invested_eur - 1) * 100

    @property
    def benchmark_return_pct(self) -> float | None:
        """Money-weighted return of the passive alternative."""
        if self.invested_eur <= 0:
            return None
        return (self.benchmark_eur / self.invested_eur - 1) * 100

    @property
    def is_meaningful(self) -> bool:
        """Whether enough time has passed for the difference to mean anything.

        Computed rather than asserted, so it stops being false on its own once
        it should. Recording starts in week one; reading it as a verdict should
        not, because a few months of difference across fourteen holdings is
        noise, and mistaking noise for skill is the error this whole system is
        built to avoid.
        """
        return self.days >= BENCHMARK_MEANINGFUL_AFTER_DAYS

    @property
    def verdict(self) -> str:
        """A sentence the reader can trust, given how long it has run."""
        if not self.is_meaningful:
            years = BENCHMARK_MEANINGFUL_AFTER_DAYS / 365
            return (
                f"Too early to mean anything — {self.days} days of "
                f"{BENCHMARK_MEANINGFUL_AFTER_DAYS} ({years:.0f} years). "
                f"Recorded now because it cannot be reconstructed later."
            )
        better = "ahead of" if self.difference_eur >= 0 else "behind"
        return (
            f"Over {self.days / 365:.1f} years the portfolio is "
            f"€{abs(self.difference_eur):,.2f} {better} the same money in "
            f"{self.ticker}."
        )


def ensure_benchmark(conn) -> int:  # type: ignore[no-untyped-def]
    """Register the benchmark security, and return its id."""
    from models import Security
    from store import upsert_security

    security_id = upsert_security(
        conn,
        Security(
            ticker=BENCHMARK_TICKER,
            name=BENCHMARK_NAME,
            currency="EUR",
            asset_class="fund",
            sector="Benchmark",
        ),
    )
    with conn:
        conn.execute(
            "UPDATE securities SET is_benchmark = 1 WHERE id = ?", (security_id,)
        )
    return security_id


def sync_benchmark(conn, *, provider=None, account_id: int = 1) -> int:  # type: ignore[no-untyped-def]
    """Fetch benchmark history from the first cash flow onwards.

    Args:
        conn: Open database connection.
        provider: Market-data source; defaults to the configured one.
        account_id: Account whose cash flows set the start date.

    Returns:
        The number of daily closes stored.
    """
    from market_data import get_provider
    from store import load_cash_flows, save_prices

    security_id = ensure_benchmark(conn)
    flows = load_cash_flows(conn, account_id=account_id)
    if not flows:
        logger.info("No cash flows yet, so no benchmark history to fetch")
        return 0

    start = min(flow.flow_date for flow in flows)
    history = (provider or get_provider()).fetch_history(BENCHMARK_TICKER, start=start)
    if not history:
        logger.warning("No benchmark history returned for %s", BENCHMARK_TICKER)
        return 0

    save_prices(
        conn,
        [
            (security_id, day, close, "EUR", "yfinance")
            for day, close in sorted(history.items())
        ],
    )
    return len(history)


def _price_on_or_before(prices: dict[str, float], day: str) -> tuple[str, float] | None:
    """Return the last known close at or before *day*.

    Markets close at weekends and on holidays, and money moves on days they are
    shut. Taking the previous close is what an investor could actually have
    done; requiring an exact match would silently drop those contributions.
    """
    candidates = [known for known in prices if known <= day]
    if not candidates:
        return None
    chosen = max(candidates)
    return chosen, prices[chosen]


def compare(conn, *, account_id: int = 1, today: date | None = None) -> Comparison:  # type: ignore[no-untyped-def]
    """Return the portfolio beside what the same money would be worth passively.

    Every euro that entered the account buys benchmark units at that day's
    close, building a shadow portfolio moved on exactly the same dates. Money
    withdrawn sells units the same way.

    Args:
        conn: Open database connection.
        account_id: Account to compare.
        today: Reference date, for tests.

    Returns:
        The comparison, naming any cash flow that could not be priced.

    Raises:
        ValueError: If the benchmark has no stored history.
    """
    from portfolio import cash_eur, holdings, total_value
    from store import load_cash_flows, load_securities

    securities = load_securities(conn)
    benchmark = securities.get(BENCHMARK_TICKER)
    if benchmark is None or benchmark.id is None:
        raise ValueError(
            f"No benchmark recorded. Run 'main.py benchmark sync' to fetch "
            f"{BENCHMARK_TICKER} history."
        )

    prices = {
        row["price_date"]: row["close_native"]
        for row in conn.execute(
            "SELECT price_date, close_native FROM prices WHERE security_id = ?",
            (benchmark.id,),
        )
    }
    if not prices:
        raise ValueError(
            f"No stored history for {BENCHMARK_TICKER}. Run "
            f"'main.py benchmark sync' first."
        )

    units = 0.0
    invested = 0.0
    unpriced: list[str] = []
    for flow in load_cash_flows(conn, account_id=account_id):
        # Dividends and fees are internal to the account: no new money arrived,
        # so the shadow portfolio must not move either.
        if flow.kind not in {"OPENING_BALANCE", "CONTRIBUTION", "WITHDRAWAL"}:
            continue
        found = _price_on_or_before(prices, flow.flow_date)
        if found is None:
            unpriced.append(flow.flow_date)
            continue
        _, close = found
        units += flow.amount_eur / close
        invested += flow.amount_eur

    rows = holdings(conn, account_id=account_id)
    portfolio = total_value(rows, cash=cash_eur(conn, account_id=account_id))
    latest_day = max(prices)

    return Comparison(
        ticker=BENCHMARK_TICKER,
        name=BENCHMARK_NAME,
        invested_eur=invested,
        portfolio_eur=portfolio,
        benchmark_eur=units * prices[latest_day],
        units=units,
        priced_on=latest_day,
        since=min(
            (flow.flow_date for flow in load_cash_flows(conn, account_id=account_id)),
            default=None,
        ),
        unpriced_flows=tuple(unpriced),
    )
