"""Tests for the record of what following the recommendations would have done."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from record import record_lines, shadow_record
from seed import load_snapshot, seed_database
from store import save_fx_rate, save_prices
from tests.test_seed import FIXTURE

TODAY = date(2026, 9, 30)


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    """AAA 110, BBB 90, CCC 150 and 5 cash, priced in EUR."""
    seed_database(conn, load_snapshot(FIXTURE))
    save_fx_rate(
        conn, rate_date="2026-09-01", base="USD", quote="EUR", rate=1.0, source="test"
    )
    return conn


def _recommend(
    conn: sqlite3.Connection,
    *,
    action: str = "ADD",
    security_id: int | None = 1,
    amount: float | None = 100.0,
    unit_eur: float | None = 10.0,
    run_date: str = "2026-09-07",
) -> int:
    cursor = conn.execute(
        """INSERT INTO recommendation (run_date, security_id, action, amount_eur,
        rationale, urgency, value_eur, expires_on, created_at)
        VALUES (?, ?, ?, ?, 'because', 'low', ?, '2099-01-01', 'x')""",
        (run_date, security_id, action, amount, unit_eur),
    )
    return int(cursor.lastrowid)


def _price(conn: sqlite3.Connection, security_id: int, close: float) -> None:
    save_prices(conn, [(security_id, "2026-09-28", close, "EUR", "test")])


class TestFollowingTrades:
    def test_a_buy_that_rose_would_have_gained(
        self, seeded: sqlite3.Connection
    ) -> None:
        _recommend(seeded)
        _price(seeded, 1, 12.0)
        trade = shadow_record(seeded, today=TODAY).trades[0]
        assert trade.delta_eur == pytest.approx(20.0)
        assert trade.delta_pct == pytest.approx(20.0)

    def test_a_sale_is_measured_by_the_fall_it_avoided(
        self, seeded: sqlite3.Connection
    ) -> None:
        # Selling at 10 and watching it fall to 8 saved a fifth of the amount.
        _recommend(seeded, action="TRIM")
        _price(seeded, 1, 8.0)
        trade = shadow_record(seeded, today=TODAY).trades[0]
        assert trade.delta_eur == pytest.approx(20.0)

    def test_a_trade_that_cannot_be_priced_is_left_out_of_the_total(
        self, seeded: sqlite3.Connection
    ) -> None:
        # An unpriceable holding must be visibly missing, never counted as zero.
        _recommend(seeded)
        _recommend(seeded, security_id=2, unit_eur=None)
        _price(seeded, 1, 12.0)
        record = shadow_record(seeded, today=TODAY)
        assert record.unpriced == 1
        assert record.trades[1].delta_eur is None
        assert "could not be priced" in " ".join(record_lines(record))

    def test_older_than_the_window_is_not_counted(
        self, seeded: sqlite3.Connection
    ) -> None:
        _recommend(seeded, run_date="2026-01-05")
        _price(seeded, 1, 12.0)
        assert shadow_record(seeded, today=TODAY, weeks=12).trades == ()


class TestFollowedOrSkipped:
    def test_a_matching_fill_counts_as_followed(
        self, seeded: sqlite3.Connection
    ) -> None:
        # Read from the ledger: syncing the broker is the only step required.
        _recommend(seeded)
        _price(seeded, 1, 12.0)
        seeded.execute(
            """INSERT INTO trades (account_id, security_id, trade_date, side, quantity,
            amount_eur, created_at) VALUES (1, 1, '2026-09-09', 'BUY', 5, 100, 'x')"""
        )
        record = shadow_record(seeded, today=TODAY)
        assert record.followed and not record.skipped

    def test_a_fill_long_after_does_not_count(self, seeded: sqlite3.Connection) -> None:
        _recommend(seeded)
        _price(seeded, 1, 12.0)
        seeded.execute(
            """INSERT INTO trades (account_id, security_id, trade_date, side, quantity,
            amount_eur, created_at) VALUES (1, 1, '2026-09-25', 'BUY', 5, 100, 'x')"""
        )
        assert shadow_record(seeded, today=TODAY).skipped

    def test_the_opening_trades_are_not_mistaken_for_following(
        self, seeded: sqlite3.Connection
    ) -> None:
        # The seed writes synthetic trades dated the snapshot; they are not fills.
        _recommend(seeded, run_date="2026-01-15")
        _price(seeded, 1, 12.0)
        record = shadow_record(seeded, today=date(2026, 1, 20), weeks=12)
        assert record.trades and record.skipped == record.trades


class TestKeepingCash:
    def _keep_cash(
        self, conn: sqlite3.Connection, run_date: str = "2026-09-07"
    ) -> None:
        _recommend(
            conn, action="KEEP_CASH", security_id=None, amount=None, run_date=run_date
        )

    def test_cash_is_measured_against_the_benchmark(
        self, seeded: sqlite3.Connection
    ) -> None:
        from config import BENCHMARK_TICKER
        from models import Security
        from store import upsert_security

        benchmark = upsert_security(
            seeded, Security(ticker=BENCHMARK_TICKER, name="Index", currency="EUR")
        )
        save_prices(
            seeded,
            [
                (benchmark, "2026-09-07", 100.0, "EUR", "test"),
                (benchmark, "2026-09-28", 110.0, "EUR", "test"),
            ],
        )
        self._keep_cash(seeded)
        cash = shadow_record(seeded, today=TODAY).cash
        assert cash.benchmark_move_pct == pytest.approx(10.0)
        # Five euro of cash against a 10% rise: fifty cents of opportunity.
        assert cash.cost_eur == pytest.approx(0.5)
        assert "cost €0.50" in " ".join(
            record_lines(shadow_record(seeded, today=TODAY))
        )

    def test_a_flat_benchmark_says_it_cost_nothing(
        self, seeded: sqlite3.Connection
    ) -> None:
        from config import BENCHMARK_TICKER
        from models import Security
        from store import upsert_security

        benchmark = upsert_security(
            seeded, Security(ticker=BENCHMARK_TICKER, name="Index", currency="EUR")
        )
        save_prices(
            seeded,
            [
                (benchmark, "2026-09-07", 100.0, "EUR", "test"),
                (benchmark, "2026-09-28", 100.0, "EUR", "test"),
            ],
        )
        self._keep_cash(seeded)
        lines = " ".join(record_lines(shadow_record(seeded, today=TODAY)))
        assert "cost nothing yet" in lines

    def test_without_benchmark_prices_it_says_so(
        self, seeded: sqlite3.Connection
    ) -> None:
        self._keep_cash(seeded)
        record = shadow_record(seeded, today=TODAY)
        assert record.cash.cost_eur is None
        assert "no benchmark price" in " ".join(record_lines(record))

    def test_nothing_recommended_reads_plainly(
        self, seeded: sqlite3.Connection
    ) -> None:
        assert record_lines(shadow_record(seeded, today=TODAY)) == [
            "No trade has been recommended yet, so there is nothing to follow."
        ]


class TestInTheReport:
    def test_the_summary_carries_the_record(self, seeded: sqlite3.Connection) -> None:
        from report import weekly_report

        _recommend(seeded)
        _price(seeded, 1, 12.0)
        body = weekly_report(seeded, today=TODAY).body
        assert "<b>If you had followed it</b>" in body
        assert "AAA add €100" in body
