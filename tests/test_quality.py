"""Tests for valuation freshness, per holding and grouped.

The grouped form exists because the per-holding form is unreadable at portfolio
size: a sync five days behind produced twenty-eight near-identical clauses on
one line, which is how a real warning gets skipped.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from models import Holding, Position, Security
from quality import valuation_gaps, valuation_summary

TODAY = date(2026, 9, 12)
FRESH = "2026-09-11"
STALE = "2026-09-01"


def _holding(
    ticker: str = "TEST",
    *,
    currency: str = "EUR",
    value: float | None = 100.0,
    priced: str | None = FRESH,
) -> Holding:
    return Holding(
        position=Position(
            security=Security(ticker=ticker, name=f"{ticker} Inc", currency=currency),
            quantity=1.0,
            cost_basis_eur=80.0,
        ),
        value_eur=value,
        price_date=priced if value is not None else None,
    )


def _rate(conn: sqlite3.Connection, currency: str, day: str) -> None:
    conn.execute(
        "INSERT INTO fx_rates VALUES (?, ?, 'EUR', 0.9, 'test', 'now')", (day, currency)
    )


class TestPerHolding:
    """The detailed form, which callers inspecting one security want."""

    def test_a_fresh_portfolio_reports_nothing(self, conn: sqlite3.Connection) -> None:
        assert valuation_gaps(conn, [_holding()], TODAY) == []

    def test_an_unpriced_holding_is_named_once(self, conn: sqlite3.Connection) -> None:
        # And not also reported as stale: it has no price to be stale.
        gaps = valuation_gaps(conn, [_holding(value=None)], TODAY)
        assert gaps == ["TEST: unpriced"]

    def test_a_stale_price_is_reported_with_its_date(
        self, conn: sqlite3.Connection
    ) -> None:
        (gap,) = valuation_gaps(conn, [_holding(priced=STALE)], TODAY)
        assert STALE in gap

    def test_a_foreign_holding_needs_a_current_rate(
        self, conn: sqlite3.Connection
    ) -> None:
        _rate(conn, "USD", STALE)
        gaps = valuation_gaps(conn, [_holding(currency="USD")], TODAY)
        assert gaps == ["TEST: USD/EUR FX missing or stale"]

    def test_a_euro_holding_needs_no_rate(self, conn: sqlite3.Connection) -> None:
        assert valuation_gaps(conn, [_holding(currency="EUR")], TODAY) == []


class TestGrouped:
    """The readable form: one line per cause, not per holding per cause."""

    def test_a_sound_valuation_says_nothing(self, conn: sqlite3.Connection) -> None:
        assert valuation_summary(conn, [_holding()], TODAY) == []

    def test_fourteen_stale_holdings_collapse_to_one_line(
        self, conn: sqlite3.Connection
    ) -> None:
        # The defect this exists for. Per holding this was fourteen clauses.
        rows = [_holding(f"T{n:02d}", priced=STALE) for n in range(14)]
        summary = valuation_summary(conn, rows, TODAY)
        assert len(summary) == 2
        assert summary[0] == "Every price is stale (oldest 2026-09-01, 11 days old)."
        assert "main.py sync" in summary[1]

    def test_one_currency_is_named_once_not_per_holding(
        self, conn: sqlite3.Connection
    ) -> None:
        # Fourteen USD holdings share one USD/EUR rate; saying so fourteen
        # times says nothing fourteen times.
        _rate(conn, "USD", STALE)
        rows = [_holding(f"T{n:02d}", currency="USD") for n in range(14)]
        summary = valuation_summary(conn, rows, TODAY)
        assert "USD/EUR rate is from 2026-09-01." in summary
        assert sum("USD/EUR" in line for line in summary) == 1

    def test_two_currencies_get_a_line_each(self, conn: sqlite3.Connection) -> None:
        _rate(conn, "USD", STALE)
        _rate(conn, "GBP", STALE)
        summary = valuation_summary(
            conn, [_holding("A", currency="USD"), _holding("B", currency="GBP")], TODAY
        )
        assert any("USD/EUR" in line for line in summary)
        assert any("GBP/EUR" in line for line in summary)

    def test_a_missing_rate_is_distinguished_from_an_old_one(
        self, conn: sqlite3.Connection
    ) -> None:
        summary = valuation_summary(conn, [_holding(currency="USD")], TODAY)
        assert "USD/EUR rate is missing." in summary

    def test_some_stale_prices_are_counted_rather_than_called_every(
        self, conn: sqlite3.Connection
    ) -> None:
        rows = [_holding("A"), _holding("B", priced=STALE), _holding("C", priced=STALE)]
        assert "2 prices are stale" in valuation_summary(conn, rows, TODAY)[0]

    def test_unpriced_holdings_are_listed_and_their_effect_stated(
        self, conn: sqlite3.Connection
    ) -> None:
        rows = [_holding("A"), _holding("B", value=None)]
        (line,) = valuation_summary(conn, rows, TODAY)
        assert "Cannot be priced: B." in line
        assert "Excluded from the total" in line

    def test_an_unpriced_holding_alone_does_not_suggest_a_sync(
        self, conn: sqlite3.Connection
    ) -> None:
        # A manually priced instrument is not waiting on the feed, so pointing
        # at sync would send the owner somewhere that cannot help.
        summary = valuation_summary(conn, [_holding(value=None)], TODAY)
        assert not any("sync" in line for line in summary)

    def test_every_cause_present_yields_one_line_each_plus_the_remedy(
        self, conn: sqlite3.Connection
    ) -> None:
        _rate(conn, "USD", STALE)
        rows = [
            _holding("A", value=None),
            _holding("B", priced=STALE),
            _holding("C", currency="USD"),
        ]
        summary = valuation_summary(conn, rows, TODAY)
        assert len(summary) == 4
        assert summary[-1].startswith("Run 'main.py sync'")

    def test_an_empty_portfolio_reports_nothing(self, conn: sqlite3.Connection) -> None:
        assert valuation_summary(conn, [], TODAY) == []


class TestSharedPredicate:
    """Both forms must agree about what stale means."""

    @pytest.mark.parametrize("priced", [FRESH, STALE, None])
    def test_the_two_forms_agree_on_whether_anything_is_wrong(
        self, conn: sqlite3.Connection, priced: str | None
    ) -> None:
        # One definition of current, two renderings of it. Two definitions
        # would disagree the first time the threshold changed.
        rows = [_holding(value=None if priced is None else 100.0, priced=priced)]
        assert bool(valuation_gaps(conn, rows, TODAY)) == bool(
            valuation_summary(conn, rows, TODAY)
        )
