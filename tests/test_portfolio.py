"""Tests for deriving positions, cash and concentration from the ledger."""

from __future__ import annotations

import sqlite3

import pytest

from models import CashFlow, Holding, Position, Security, Trade
from portfolio import cash_eur, concentration, holdings, positions, total_value
from store import insert_cash_flow, insert_trade, save_snapshot


def _buy(
    conn: sqlite3.Connection, security_id: int, qty: float, eur: float, **kw
) -> None:
    insert_trade(
        conn,
        Trade(
            security_id=security_id,
            trade_date=kw.pop("date", "2026-01-01"),
            side="BUY",
            quantity=qty,
            amount_eur=eur,
            **kw,
        ),
    )


def _sell(
    conn: sqlite3.Connection, security_id: int, qty: float, eur: float, **kw
) -> None:
    insert_trade(
        conn,
        Trade(
            security_id=security_id,
            trade_date=kw.pop("date", "2026-02-01"),
            side="SELL",
            quantity=qty,
            amount_eur=eur,
            **kw,
        ),
    )


class TestPositions:
    """Positions are derived, never stored."""

    def test_single_buy(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        _buy(conn, security_id, 2.0, 100.0)
        (position,) = positions(conn)
        assert position.quantity == 2.0
        assert position.cost_basis_eur == 100.0
        assert position.avg_cost_eur == 50.0

    def test_fees_join_the_cost_basis(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        _buy(conn, security_id, 2.0, 100.0, fee_eur=1.5)
        (position,) = positions(conn)
        assert position.cost_basis_eur == 101.5

    def test_averages_across_buys(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        _buy(conn, security_id, 1.0, 50.0)
        _buy(conn, security_id, 3.0, 210.0, date="2026-01-15")
        (position,) = positions(conn)
        assert position.quantity == 4.0
        assert position.cost_basis_eur == 260.0
        assert position.avg_cost_eur == 65.0

    def test_partial_sell_releases_proportional_cost(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        _buy(conn, security_id, 4.0, 200.0)
        _sell(conn, security_id, 1.0, 70.0)
        (position,) = positions(conn)
        assert position.quantity == 3.0
        assert position.cost_basis_eur == pytest.approx(150.0)
        # Released basis was 50; sold for 70.
        assert position.realised_pnl_eur == pytest.approx(20.0)

    def test_full_sell_closes_the_position(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        _buy(conn, security_id, 4.0, 200.0)
        _sell(conn, security_id, 4.0, 250.0)
        assert positions(conn) == []

    def test_dust_quantity_is_treated_as_closed(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        _buy(conn, security_id, 1.0, 50.0)
        _sell(conn, security_id, 1.0 - 1e-12, 50.0)
        assert positions(conn) == []

    def test_oversell_cannot_produce_negative_basis(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        _buy(conn, security_id, 1.0, 50.0)
        _sell(conn, security_id, 5.0, 300.0)
        assert positions(conn) == []

    def test_sell_without_position_is_ignored(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        _sell(conn, security_id, 1.0, 50.0)
        assert positions(conn) == []


class TestCash:
    """Cash follows contributions and trades."""

    def test_contribution_less_purchase(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        insert_cash_flow(
            conn,
            CashFlow(flow_date="2026-01-01", kind="CONTRIBUTION", amount_eur=200.0),
        )
        _buy(conn, security_id, 1.0, 150.0, fee_eur=2.0)
        assert cash_eur(conn) == pytest.approx(48.0)

    def test_sell_returns_cash_net_of_fees(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        insert_cash_flow(
            conn,
            CashFlow(flow_date="2026-01-01", kind="CONTRIBUTION", amount_eur=100.0),
        )
        _buy(conn, security_id, 1.0, 100.0)
        _sell(conn, security_id, 1.0, 120.0, fee_eur=1.0)
        assert cash_eur(conn) == pytest.approx(119.0)

    def test_withdrawal_is_stored_negative(
        self, conn: sqlite3.Connection, account_id: int
    ) -> None:
        insert_cash_flow(
            conn,
            CashFlow(flow_date="2026-01-01", kind="CONTRIBUTION", amount_eur=100.0),
        )
        insert_cash_flow(
            conn, CashFlow(flow_date="2026-02-01", kind="WITHDRAWAL", amount_eur=-30.0)
        )
        assert cash_eur(conn) == pytest.approx(70.0)


class TestValuation:
    """Pricing falls back from feed, to snapshot, to unknown."""

    def test_uses_stored_price_and_fx(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        _buy(conn, security_id, 2.0, 100.0)
        conn.execute(
            "INSERT INTO prices VALUES (?, '2026-03-01', 60.0, 'USD', 'test', 'now')",
            (security_id,),
        )
        conn.execute(
            "INSERT INTO fx_rates VALUES ('2026-03-01', 'USD', 'EUR', 0.9, 'test', 'now')"
        )
        (row,) = holdings(conn, account_id=account_id)
        assert row.value_eur == pytest.approx(108.0)
        assert row.price_source == "test"

    def test_falls_back_to_snapshot_unit_value(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        _buy(conn, security_id, 2.0, 100.0)
        save_snapshot(
            conn,
            account_id=account_id,
            snapshot_date="2026-02-01",
            cash_eur=0.0,
            positions=[(security_id, 2.0, 130.0, 30.0)],
            source="revolut",
        )
        (row,) = holdings(conn, account_id=account_id)
        assert row.value_eur == pytest.approx(130.0)
        assert row.price_source == "revolut"

    def test_snapshot_unit_value_rescales_to_current_quantity(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        _buy(conn, security_id, 2.0, 100.0)
        save_snapshot(
            conn,
            account_id=account_id,
            snapshot_date="2026-02-01",
            cash_eur=0.0,
            positions=[(security_id, 2.0, 130.0, 30.0)],
            source="revolut",
        )
        _buy(conn, security_id, 2.0, 120.0, date="2026-02-15")
        (row,) = holdings(conn, account_id=account_id)
        assert row.value_eur == pytest.approx(260.0)

    def test_missing_fx_rate_does_not_price_at_zero(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        _buy(conn, security_id, 2.0, 100.0)
        conn.execute(
            "INSERT INTO prices VALUES (?, '2026-03-01', 60.0, 'USD', 'test', 'now')",
            (security_id,),
        )
        (row,) = holdings(conn, account_id=account_id)
        assert row.value_eur is None

    def test_unpriced_holding_is_excluded_from_total(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        _buy(conn, security_id, 2.0, 100.0)
        rows = holdings(conn, account_id=account_id)
        assert total_value(rows, cash=10.0) == pytest.approx(10.0)

    def test_unrealised_pnl_is_none_when_unpriced(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        _buy(conn, security_id, 2.0, 100.0)
        (row,) = holdings(conn, account_id=account_id)
        assert row.unrealised_pnl_eur is None
        assert row.unrealised_return_pct is None


def _holding(
    ticker: str, value: float, sector: str, themes: tuple[str, ...]
) -> Holding:
    return Holding(
        position=Position(
            security=Security(
                id=1, ticker=ticker, name=ticker, sector=sector, themes=themes
            ),
            quantity=1.0,
            cost_basis_eur=value,
        ),
        value_eur=value,
    )


class TestConcentration:
    """Grouping by security, sector and overlapping themes."""

    ROWS = [
        _holding("AAPL", 50.0, "Information Technology", ("mega-cap-tech",)),
        _holding("NVDA", 30.0, "Information Technology", ("mega-cap-tech", "ai-semis")),
        _holding("LLY", 20.0, "Health Care", ("healthcare",)),
    ]

    def test_security_weights(self) -> None:
        groups = concentration(self.ROWS, key="security", total=100.0)
        assert [(g.label, g.weight_pct) for g in groups] == [
            ("AAPL", 50.0),
            ("NVDA", 30.0),
            ("LLY", 20.0),
        ]

    def test_sector_groups_members(self) -> None:
        groups = concentration(self.ROWS, key="sector", total=100.0)
        assert groups[0].label == "Information Technology"
        assert groups[0].weight_pct == 80.0
        assert groups[0].members == ("AAPL", "NVDA")

    def test_theme_weights_may_overlap(self) -> None:
        groups = concentration(self.ROWS, key="theme", total=100.0)
        by_label = {group.label: group.weight_pct for group in groups}
        # NVDA counts fully in both of its themes, so the total exceeds 100.
        assert by_label["mega-cap-tech"] == 80.0
        assert by_label["ai-semis"] == 30.0
        assert sum(by_label.values()) > 100.0

    def test_over_limit_flag_uses_the_security_limit(self) -> None:
        groups = concentration(self.ROWS, key="security", total=100.0)
        assert groups[0].over_limit is True  # AAPL 50% > 20% position limit
        assert groups[2].over_limit is False  # LLY 20% is at, not over, the limit

    def test_unpriced_holdings_are_skipped(self) -> None:
        rows = [
            *self.ROWS,
            Holding(
                position=Position(
                    security=Security(id=9, ticker="SPCX", name="SpaceX"),
                    quantity=1.0,
                    cost_basis_eur=40.0,
                ),
                value_eur=None,
            ),
        ]
        groups = concentration(rows, key="security", total=100.0)
        assert "SPCX" not in [group.label for group in groups]

    def test_untagged_security_gets_a_label(self) -> None:
        rows = [_holding("XYZ", 10.0, "Industrials", ())]
        groups = concentration(rows, key="theme", total=10.0)
        assert groups[0].label == "untagged"

    def test_unclassified_sector_gets_a_label(self) -> None:
        row = _holding("XYZ", 10.0, "Industrials", ())
        bare = Holding(
            position=Position(
                security=Security(id=2, ticker="XYZ", name="XYZ"),
                quantity=1.0,
                cost_basis_eur=10.0,
            ),
            value_eur=10.0,
        )
        assert concentration([row], key="sector", total=10.0)[0].label == "Industrials"
        assert (
            concentration([bare], key="sector", total=10.0)[0].label == "Unclassified"
        )

    def test_zero_total_does_not_divide_by_zero(self) -> None:
        groups = concentration(self.ROWS, key="security", total=0.0)
        assert all(group.weight_pct == 0.0 for group in groups)

    def test_unknown_key_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unknown grouping"):
            concentration(self.ROWS, key="colour", total=100.0)
