"""Tests for the views the chat agent queries.

These exist because the agent writes its own SQL against them. A view that
quietly drops a fee, or names a column the agent does not expect, produces a
wrong answer that reads as a confident one — so the shape is pinned here as
firmly as the arithmetic.
"""

from __future__ import annotations

import sqlite3

import pytest

from models import CashFlow, Security, Trade
from portfolio import cash_eur
from store import insert_cash_flow, insert_trade, upsert_security


def _flow(conn: sqlite3.Connection, kind: str, eur: float, date: str) -> None:
    insert_cash_flow(
        conn, CashFlow(flow_date=date, kind=kind, amount_eur=eur, note=None)
    )


def _trade(
    conn: sqlite3.Connection,
    security_id: int,
    side: str,
    qty: float,
    eur: float,
    *,
    fee: float = 0.0,
    date: str = "2026-01-02",
) -> None:
    insert_trade(
        conn,
        Trade(
            security_id=security_id,
            trade_date=date,
            side=side,
            quantity=qty,
            amount_eur=eur,
            fee_eur=fee,
        ),
    )


class TestCashLedgerView:
    """Every EUR movement in one column, signed once."""

    def test_a_buy_costs_its_fee_and_a_sell_returns_net(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        _flow(conn, "CONTRIBUTION", 200.0, "2026-01-01")
        _trade(conn, security_id, "BUY", 1.0, 100.0, fee=2.0)
        _trade(conn, security_id, "SELL", 1.0, 120.0, fee=1.0, date="2026-02-01")
        deltas = [
            row["delta_eur"]
            for row in conn.execute(
                "SELECT delta_eur FROM v_cash_ledger ORDER BY entry_date"
            )
        ]
        assert deltas == pytest.approx([200.0, -102.0, 119.0])

    def test_the_view_and_cash_eur_agree(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        # The point of the view: one definition of cash, not two.
        _flow(conn, "CONTRIBUTION", 250.0, "2026-01-01")
        _flow(conn, "WITHDRAWAL", -30.0, "2026-01-05")
        _flow(conn, "DIVIDEND", 1.25, "2026-01-06")
        _trade(conn, security_id, "BUY", 3.0, 90.0, fee=0.5)
        (total,) = conn.execute("SELECT SUM(delta_eur) FROM v_cash_ledger").fetchone()
        assert cash_eur(conn) == pytest.approx(total)
        assert cash_eur(conn) == pytest.approx(130.75)

    def test_a_trade_entry_carries_its_ticker_and_a_flow_does_not(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        _flow(conn, "CONTRIBUTION", 50.0, "2026-01-01")
        _trade(conn, security_id, "BUY", 1.0, 10.0)
        tickers = {
            row["kind"]: row["ticker"]
            for row in conn.execute("SELECT kind, ticker FROM v_cash_ledger")
        }
        assert tickers == {"CONTRIBUTION": None, "BUY": "TEST"}


class TestCashBalanceView:
    """The balance an account reports, and what it reports when empty."""

    def test_an_account_with_no_ledger_is_absent_not_zero(
        self, conn: sqlite3.Connection, account_id: int
    ) -> None:
        # Absent rather than zero keeps the view honest about what it knows;
        # cash_eur coalesces, which is where zero is the right answer.
        assert conn.execute("SELECT COUNT(*) FROM v_cash_balance").fetchone()[0] == 0
        assert cash_eur(conn, account_id=account_id) == pytest.approx(0.0)

    def test_balances_are_per_account(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        from models import Account
        from store import ensure_account

        second = ensure_account(
            conn,
            Account(name="other", broker="Other", currency="EUR", sync_mode="manual"),
        )
        _flow(conn, "CONTRIBUTION", 100.0, "2026-01-01")
        insert_cash_flow(
            conn,
            CashFlow(
                account_id=second,
                flow_date="2026-01-01",
                kind="CONTRIBUTION",
                amount_eur=40.0,
            ),
        )
        assert cash_eur(conn, account_id=account_id) == pytest.approx(100.0)
        assert cash_eur(conn, account_id=second) == pytest.approx(40.0)
        assert cash_eur(conn) == pytest.approx(140.0)


class TestTradesView:
    """The ledger with tickers spelled out, so no query has to guess a join."""

    def test_trades_carry_security_detail(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        _trade(conn, security_id, "BUY", 2.0, 100.0, fee=1.0)
        row = conn.execute("SELECT * FROM v_trades").fetchone()
        assert row["ticker"] == "TEST"
        assert row["security_currency"] == "USD"
        assert row["side"] == "BUY"
        assert row["fee_eur"] == pytest.approx(1.0)

    def test_each_trade_appears_once(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        # A join that fans out would inflate every SUM the agent writes.
        upsert_security(
            conn,
            Security(ticker="OTHER", name="Other", currency="EUR", feed_symbol="OTHER"),
        )
        _trade(conn, security_id, "BUY", 1.0, 10.0)
        _trade(conn, security_id, "SELL", 1.0, 12.0, date="2026-03-01")
        assert conn.execute("SELECT COUNT(*) FROM v_trades").fetchone()[0] == 2


class TestLatestPriceView:
    """The newest stored close per security, in native currency."""

    def test_only_the_newest_price_survives(
        self, conn: sqlite3.Connection, security_id: int
    ) -> None:
        for day, close in (("2026-03-01", 60.0), ("2026-03-05", 64.0)):
            conn.execute(
                "INSERT INTO prices VALUES (?, ?, ?, 'USD', 'test', 'now')",
                (security_id, day, close),
            )
        rows = conn.execute("SELECT * FROM v_latest_price").fetchall()
        assert len(rows) == 1
        assert rows[0]["price_date"] == "2026-03-05"
        assert rows[0]["close_native"] == pytest.approx(64.0)
        assert rows[0]["ticker"] == "TEST"

    def test_an_unpriced_security_is_absent(
        self, conn: sqlite3.Connection, security_id: int
    ) -> None:
        # The view says nothing about EUR value, so absence here is a missing
        # price, never a zero valuation. Converting it is portfolio.holdings'
        # job precisely so the None survives.
        assert conn.execute("SELECT COUNT(*) FROM v_latest_price").fetchone()[0] == 0
