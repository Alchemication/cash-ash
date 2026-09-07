"""Tests for the passive comparison.

The comparison is money-weighted, which is the whole point: a return-versus-
return figure silently assumes every euro was present from the start, and none
of them were.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from benchmark import Comparison, compare, ensure_benchmark, sync_benchmark
from config import BENCHMARK_MEANINGFUL_AFTER_DAYS, BENCHMARK_TICKER
from models import CashFlow
from seed import load_snapshot, seed_database
from store import insert_cash_flow, save_prices
from tests.test_seed import FIXTURE


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_database(conn, load_snapshot(FIXTURE))
    return conn


def _price_benchmark(conn: sqlite3.Connection, prices: dict[str, float]) -> int:
    security_id = ensure_benchmark(conn)
    save_prices(
        conn,
        [(security_id, day, close, "EUR", "test") for day, close in prices.items()],
    )
    return security_id


class _Provider:
    """A market-data source returning a fixed history."""

    name = "fake"

    def __init__(self, history: dict[str, float]) -> None:
        self._history = history
        self.asked: list[str] = []

    def fetch_history(self, symbol: str, *, start: str) -> dict[str, float]:
        self.asked.append(start)
        return self._history


class TestSync:
    """History starts where the money did."""

    def test_fetches_from_the_first_cash_flow(self, seeded: sqlite3.Connection) -> None:
        provider = _Provider({"2026-01-15": 100.0})
        sync_benchmark(seeded, provider=provider)
        assert provider.asked == ["2026-01-15"]

    def test_stores_every_close(self, seeded: sqlite3.Connection) -> None:
        history = {"2026-01-15": 100.0, "2026-01-16": 101.0}
        assert sync_benchmark(seeded, provider=_Provider(history)) == 2

    def test_no_cash_flows_fetches_nothing(self, conn: sqlite3.Connection) -> None:
        provider = _Provider({"2026-01-15": 100.0})
        assert sync_benchmark(conn, provider=provider) == 0
        assert provider.asked == []

    def test_an_empty_history_is_not_an_error(self, seeded: sqlite3.Connection) -> None:
        # Absent data must not be recorded as a flat market.
        assert sync_benchmark(seeded, provider=_Provider({})) == 0

    def test_the_benchmark_is_flagged(self, seeded: sqlite3.Connection) -> None:
        # Price syncing asks what is held, and a benchmark deliberately is not.
        ensure_benchmark(seeded)
        row = seeded.execute(
            "SELECT is_benchmark FROM securities WHERE ticker = ?",
            (BENCHMARK_TICKER,),
        ).fetchone()
        assert row["is_benchmark"] == 1

    def test_the_benchmark_holds_no_position(self, seeded: sqlite3.Connection) -> None:
        from portfolio import positions

        ensure_benchmark(seeded)
        held = {p.security.ticker for p in positions(seeded)}
        assert BENCHMARK_TICKER not in held


class TestMoneyWeighting:
    """Each euro buys units on the day it arrived."""

    def test_a_single_flow_buys_at_that_days_price(
        self, conn: sqlite3.Connection
    ) -> None:
        from models import Account
        from store import ensure_account

        ensure_account(
            conn, Account(name="a", broker="b", currency="EUR", sync_mode="manual")
        )
        insert_cash_flow(
            conn,
            CashFlow(flow_date="2026-01-15", kind="CONTRIBUTION", amount_eur=100.0),
        )
        _price_benchmark(conn, {"2026-01-15": 100.0, "2026-02-15": 110.0})
        result = compare(conn)
        assert result.units == pytest.approx(1.0)
        assert result.benchmark_eur == pytest.approx(110.0)

    def test_later_money_does_not_earn_earlier_returns(
        self, conn: sqlite3.Connection
    ) -> None:
        # The error a return-versus-return figure makes: money that arrived in
        # February cannot have captured January's rise.
        from models import Account
        from store import ensure_account

        ensure_account(
            conn, Account(name="a", broker="b", currency="EUR", sync_mode="manual")
        )
        insert_cash_flow(
            conn,
            CashFlow(flow_date="2026-01-15", kind="CONTRIBUTION", amount_eur=100.0),
        )
        insert_cash_flow(
            conn,
            CashFlow(flow_date="2026-02-15", kind="CONTRIBUTION", amount_eur=100.0),
        )
        _price_benchmark(conn, {"2026-01-15": 100.0, "2026-02-15": 200.0})
        result = compare(conn)
        # One unit bought at 100, half a unit at 200: 1.5 units at 200 = 300.
        assert result.units == pytest.approx(1.5)
        assert result.benchmark_eur == pytest.approx(300.0)
        assert result.invested_eur == pytest.approx(200.0)

    def test_a_weekend_flow_uses_the_previous_close(
        self, conn: sqlite3.Connection
    ) -> None:
        # Markets shut at weekends and money still moves; requiring an exact
        # match would silently drop those contributions.
        from models import Account
        from store import ensure_account

        ensure_account(
            conn, Account(name="a", broker="b", currency="EUR", sync_mode="manual")
        )
        insert_cash_flow(
            conn,
            CashFlow(flow_date="2026-01-17", kind="CONTRIBUTION", amount_eur=100.0),
        )
        _price_benchmark(conn, {"2026-01-16": 50.0})
        result = compare(conn)
        assert result.units == pytest.approx(2.0)
        assert result.unpriced_flows == ()

    def test_a_flow_before_any_price_is_reported(
        self, conn: sqlite3.Connection
    ) -> None:
        from models import Account
        from store import ensure_account

        ensure_account(
            conn, Account(name="a", broker="b", currency="EUR", sync_mode="manual")
        )
        insert_cash_flow(
            conn,
            CashFlow(flow_date="2025-01-01", kind="CONTRIBUTION", amount_eur=100.0),
        )
        _price_benchmark(conn, {"2026-01-16": 50.0})
        result = compare(conn)
        assert result.unpriced_flows == ("2025-01-01",)
        assert result.units == 0.0

    def test_a_withdrawal_sells_units(self, conn: sqlite3.Connection) -> None:
        from models import Account
        from store import ensure_account

        ensure_account(
            conn, Account(name="a", broker="b", currency="EUR", sync_mode="manual")
        )
        insert_cash_flow(
            conn,
            CashFlow(flow_date="2026-01-15", kind="CONTRIBUTION", amount_eur=200.0),
        )
        insert_cash_flow(
            conn,
            CashFlow(flow_date="2026-01-16", kind="WITHDRAWAL", amount_eur=-100.0),
        )
        _price_benchmark(conn, {"2026-01-15": 100.0, "2026-01-16": 100.0})
        assert compare(conn).units == pytest.approx(1.0)

    def test_dividends_and_fees_do_not_move_the_shadow(
        self, conn: sqlite3.Connection
    ) -> None:
        # No new money arrived, so the passive alternative must not change.
        from models import Account
        from store import ensure_account

        ensure_account(
            conn, Account(name="a", broker="b", currency="EUR", sync_mode="manual")
        )
        insert_cash_flow(
            conn,
            CashFlow(flow_date="2026-01-15", kind="CONTRIBUTION", amount_eur=100.0),
        )
        insert_cash_flow(
            conn, CashFlow(flow_date="2026-01-16", kind="DIVIDEND", amount_eur=5.0)
        )
        insert_cash_flow(
            conn, CashFlow(flow_date="2026-01-16", kind="FEE", amount_eur=-2.0)
        )
        _price_benchmark(conn, {"2026-01-15": 100.0})
        assert compare(conn).units == pytest.approx(1.0)


class TestPreconditions:
    """The comparison refuses to invent a baseline."""

    def test_no_benchmark_recorded(self, seeded: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="No benchmark recorded"):
            compare(seeded)

    def test_no_history_stored(self, seeded: sqlite3.Connection) -> None:
        ensure_benchmark(seeded)
        with pytest.raises(ValueError, match="No stored history"):
            compare(seeded)


class TestMeaningfulness:
    """The number is recorded from week one and readable from year three."""

    def _comparison(self, days: int) -> Comparison:
        start = date(2026, 1, 1)
        end = date.fromordinal(start.toordinal() + days)
        return Comparison(
            ticker="X",
            name="X",
            invested_eur=1000.0,
            portfolio_eur=1100.0,
            benchmark_eur=1050.0,
            units=1.0,
            priced_on=end.isoformat(),
            since=start.isoformat(),
        )

    def test_a_few_days_means_nothing(self) -> None:
        result = self._comparison(5)
        assert result.is_meaningful is False
        assert "Too early" in result.verdict

    def test_three_years_is_readable(self) -> None:
        # Computed rather than asserted, so it stops being false on its own.
        result = self._comparison(BENCHMARK_MEANINGFUL_AFTER_DAYS)
        assert result.is_meaningful is True
        assert "ahead of" in result.verdict

    def test_the_verdict_names_the_direction(self) -> None:
        result = Comparison(
            ticker="X",
            name="X",
            invested_eur=1000.0,
            portfolio_eur=900.0,
            benchmark_eur=1050.0,
            units=1.0,
            priced_on="2030-01-01",
            since="2026-01-01",
        )
        assert "behind" in result.verdict

    def test_returns_are_money_weighted(self) -> None:
        result = self._comparison(10)
        assert result.portfolio_return_pct == pytest.approx(10.0)
        assert result.benchmark_return_pct == pytest.approx(5.0)
        assert result.difference_eur == pytest.approx(50.0)

    def test_no_money_invested_yields_no_return(self) -> None:
        result = Comparison(
            ticker="X",
            name="X",
            invested_eur=0.0,
            portfolio_eur=0.0,
            benchmark_eur=0.0,
            units=0.0,
            priced_on=None,
        )
        assert result.portfolio_return_pct is None
        assert result.days == 0
