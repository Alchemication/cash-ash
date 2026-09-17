"""Tests for importing statement activity into the ledger."""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from portfolio import cash_eur, positions
from revolut_fills import import_activity, parse_activity
from revolut_statement import CurrencySection, Statement, StatementTransaction
from seed import load_snapshot, seed_database
from store import save_fx_rate
from tests.test_seed import FIXTURE


class _Provider:
    """A market data stand-in that returns fixed FX history."""

    def __init__(self, history: dict[str, float] | None = None) -> None:
        self.history = history or {}
        self.asked: list[str] = []

    def fetch_history(self, symbol: str, *, start: str) -> dict[str, float]:
        self.asked.append(symbol)
        return self.history


def _statement(*rows: StatementTransaction, currency: str = "USD") -> Statement:
    section = CurrencySection(currency)
    section.transactions.extend(rows)
    return Statement(
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
        generated_on=date(2026, 9, 30),
        sections=[section],
    )


def _row(
    details: str, *, value: str = "200", day: str = "16 Sep 2026"
) -> StatementTransaction:
    return StatementTransaction(
        timestamp=f"{day} 13:00:00 GMT",
        details=details,
        value=Decimal(value),
        fees=Decimal("1"),
        commission=Decimal("0"),
    )


@pytest.fixture
def book(conn: sqlite3.Connection) -> sqlite3.Connection:
    """The seeded book, with a USD rate stored for the trade date."""
    seed_database(conn, load_snapshot(FIXTURE))
    save_fx_rate(
        conn, rate_date="2026-09-16", base="USD", quote="EUR", rate=0.5, source="test"
    )
    return conn


class TestParsing:
    def test_a_filled_order_is_read_as_a_trade(self) -> None:
        fills, payouts, unknown = parse_activity(
            _statement(_row("AAA Market 4 US$50 Buy"))
        )
        assert not payouts and not unknown
        assert fills[0].side == "BUY"
        assert fills[0].quantity == Decimal("4")
        assert fills[0].price_native == Decimal("50")
        assert fills[0].traded_on == date(2026, 9, 16)

    def test_a_dividend_is_read_as_a_payout(self) -> None:
        _, payouts, unknown = parse_activity(
            _statement(_row("AAA Dividend", value="5"))
        )
        assert not unknown
        # Fees come out of what reaches the account.
        assert payouts[0].amount_native == Decimal("4")

    def test_an_unknown_row_is_reported_not_guessed(self) -> None:
        # A misparsed fill is a wrong position that looks right.
        fills, payouts, unknown = parse_activity(_statement(_row("AAA Custody fee")))
        assert not fills and not payouts
        assert "row not recognised" in unknown[0]


class TestImporting:
    def test_a_buy_becomes_a_trade_at_the_rate_of_its_day(
        self, book: sqlite3.Connection
    ) -> None:
        result = import_activity(book, _statement(_row("AAA Market 4 US$50 Buy")))
        assert result.trades == 1
        row = book.execute(
            "SELECT * FROM trades WHERE source_ref IS NOT NULL"
        ).fetchone()
        assert row["side"] == "BUY" and row["quantity"] == 4
        # 200 USD at 0.5, and the dollar fee with it.
        assert row["amount_eur"] == pytest.approx(100.0)
        assert row["fee_eur"] == pytest.approx(0.5)
        assert row["price_native"] == 50 and row["fx_rate"] == 0.5

    def test_a_sale_reduces_the_position(self, book: sqlite3.Connection) -> None:
        import_activity(book, _statement(_row("AAA Market 1 US$50 Sell", value="50")))
        held = {p.security.ticker: p.quantity for p in positions(book, account_id=1)}
        assert held["AAA"] == pytest.approx(1.0)

    def test_the_same_statement_twice_imports_once(
        self, book: sqlite3.Connection
    ) -> None:
        # Overlapping statements are normal, and re-downloading a month is how
        # a failed refresh is repaired.
        statement = _statement(_row("AAA Market 4 US$50 Buy"))
        import_activity(book, statement)
        second = import_activity(book, statement)
        assert second.trades == 0 and second.already == 1
        assert book.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 4

    def test_a_fill_before_the_opening_snapshot_is_left_alone(
        self, book: sqlite3.Connection
    ) -> None:
        # The seed already represents everything held on its date.
        result = import_activity(
            book, _statement(_row("AAA Market 4 US$50 Buy", day="05 Jan 2026"))
        )
        assert result.trades == 0
        assert "opening snapshot" in result.skipped[0]

    def test_an_unknown_symbol_is_skipped(self, book: sqlite3.Connection) -> None:
        result = import_activity(book, _statement(_row("ZZZ Market 4 US$50 Buy")))
        assert result.trades == 0
        assert "not a known holding" in result.skipped[0]

    def test_a_dividend_becomes_cash(self, book: sqlite3.Connection) -> None:
        before = cash_eur(book, account_id=1)
        result = import_activity(book, _statement(_row("AAA Dividend", value="5")))
        assert result.dividends == 1
        # 5 USD less the 1 USD fee, at 0.5.
        assert cash_eur(book, account_id=1) == pytest.approx(before + 2.0)


class TestExchangeRates:
    def test_a_missing_rate_skips_the_fill_rather_than_guessing(
        self, conn: sqlite3.Connection
    ) -> None:
        # Converting at today's rate would restate what the position cost.
        seed_database(conn, load_snapshot(FIXTURE))
        result = import_activity(
            conn, _statement(_row("AAA Market 4 US$50 Buy")), provider=_Provider()
        )
        assert result.trades == 0
        assert "no USD rate for that day" in result.skipped[0]
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 3

    def test_the_rate_is_fetched_for_the_trade_date_and_kept(
        self, conn: sqlite3.Connection
    ) -> None:
        seed_database(conn, load_snapshot(FIXTURE))
        provider = _Provider({"2026-09-16": 0.5, "2026-09-17": 0.9})
        result = import_activity(
            conn, _statement(_row("AAA Market 4 US$50 Buy")), provider=provider
        )
        assert result.trades == 1
        assert provider.asked == ["USDEUR=X"]
        # The rate of the trade date, not the later one.
        row = conn.execute(
            "SELECT amount_eur FROM trades WHERE source_ref IS NOT NULL"
        ).fetchone()
        assert row["amount_eur"] == pytest.approx(100.0)
        stored = conn.execute(
            "SELECT rate FROM fx_rates WHERE base='USD' AND rate_date='2026-09-16'"
        ).fetchone()
        assert stored["rate"] == 0.5
