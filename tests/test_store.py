"""Round-trip tests for the persistence layer and migration runner."""

from __future__ import annotations

import sqlite3

import pytest

from db.migrations import apply_migrations, discover_migrations, list_migrations
from models import Account, CashFlow, Security, Trade
from store import (
    ensure_account,
    insert_cash_flow,
    insert_trade,
    latest_prices,
    latest_snapshot,
    load_cash_flows,
    load_securities,
    load_trades,
    open_existing_db,
    save_snapshot,
    upsert_security,
)


class TestMigrations:
    """Migrations apply once and are recorded."""

    def test_all_discovered_migrations_apply(self, conn: sqlite3.Connection) -> None:
        statuses = list_migrations(conn)
        assert statuses
        assert all(status.status == "applied" for status in statuses)

    def test_reapplying_is_a_no_op(self, conn: sqlite3.Connection) -> None:
        assert apply_migrations(conn) == []

    def test_every_migration_has_a_name(self) -> None:
        for migration in discover_migrations():
            assert migration.name
            assert migration.name != migration.key


class TestAccounts:
    """Accounts are matched on name."""

    def test_ensure_is_idempotent(self, conn: sqlite3.Connection) -> None:
        account = Account(
            name="revolut", broker="Revolut", currency="EUR", sync_mode="manual"
        )
        first = ensure_account(conn, account)
        assert ensure_account(conn, account) == first
        row = conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()
        assert row["n"] == 1

    def test_unknown_sync_mode_is_rejected(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            ensure_account(
                conn,
                Account(name="x", broker="X", currency="EUR", sync_mode="telepathy"),
            )


class TestSecurities:
    """Securities round-trip with their theme tags."""

    def test_round_trip(self, conn: sqlite3.Connection, security_id: int) -> None:
        security = load_securities(conn)["TEST"]
        assert security.name == "Test Corp"
        assert security.currency == "USD"
        assert security.themes == ("ai-semis", "mega-cap-tech")

    def test_price_symbol_prefers_feed_symbol(self) -> None:
        assert (
            Security(ticker="BRK.B", name="B", feed_symbol="BRK-B").price_symbol
            == "BRK-B"
        )
        assert Security(ticker="AAPL", name="A").price_symbol == "AAPL"

    def test_upsert_replaces_themes_rather_than_merging(
        self, conn: sqlite3.Connection, security_id: int
    ) -> None:
        upsert_security(
            conn, Security(ticker="TEST", name="Test Corp", themes=("value",))
        )
        assert load_securities(conn)["TEST"].themes == ("value",)

    def test_upsert_keeps_the_same_id(
        self, conn: sqlite3.Connection, security_id: int
    ) -> None:
        again = upsert_security(conn, Security(ticker="TEST", name="Renamed Corp"))
        assert again == security_id
        assert load_securities(conn)["TEST"].name == "Renamed Corp"

    def test_unknown_asset_class_is_rejected(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            upsert_security(
                conn, Security(ticker="X", name="X", asset_class="beanie-babies")
            )


class TestTradesAndCashFlows:
    """Ledger rows round-trip in application order."""

    def test_trade_round_trip(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        insert_trade(
            conn,
            Trade(
                security_id=security_id,
                trade_date="2026-01-01",
                side="BUY",
                quantity=1.5,
                amount_eur=99.0,
                price_native=70.0,
                fx_rate=0.94,
                fee_eur=0.5,
                is_synthetic=True,
                note="hello",
            ),
        )
        (trade,) = load_trades(conn)
        assert trade.quantity == 1.5
        assert trade.price_native == 70.0
        assert trade.is_synthetic is True
        assert trade.note == "hello"

    def test_trades_load_oldest_first(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        for date in ("2026-03-01", "2026-01-01", "2026-02-01"):
            insert_trade(
                conn,
                Trade(
                    security_id=security_id,
                    trade_date=date,
                    side="BUY",
                    quantity=1.0,
                    amount_eur=10.0,
                ),
            )
        assert [trade.trade_date for trade in load_trades(conn)] == [
            "2026-01-01",
            "2026-02-01",
            "2026-03-01",
        ]

    def test_zero_quantity_trade_is_rejected(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            insert_trade(
                conn,
                Trade(
                    security_id=security_id,
                    trade_date="2026-01-01",
                    side="BUY",
                    quantity=0.0,
                    amount_eur=10.0,
                ),
            )

    def test_unknown_cash_flow_kind_is_rejected(
        self, conn: sqlite3.Connection, account_id: int
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            insert_cash_flow(
                conn,
                CashFlow(flow_date="2026-01-01", kind="VIBES", amount_eur=1.0),
            )

    def test_cash_flow_round_trip(
        self, conn: sqlite3.Connection, account_id: int
    ) -> None:
        insert_cash_flow(
            conn,
            CashFlow(
                flow_date="2026-01-01",
                kind="CONTRIBUTION",
                amount_eur=150.0,
                note="monthly",
            ),
        )
        (flow,) = load_cash_flows(conn)
        assert flow.kind == "CONTRIBUTION"
        assert flow.amount_eur == 150.0
        assert flow.note == "monthly"


class TestSnapshots:
    """Snapshots are replaceable and carry their positions."""

    def test_round_trip(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        save_snapshot(
            conn,
            account_id=account_id,
            snapshot_date="2026-09-02",
            cash_eur=4.15,
            positions=[(security_id, 0.5, 100.0, 12.5)],
            source="revolut",
        )
        found = latest_snapshot(conn, account_id=account_id)
        assert found is not None
        snapshot, rows = found
        assert snapshot["total_value_eur"] == pytest.approx(104.15)
        assert snapshot["positions_value_eur"] == pytest.approx(100.0)
        assert rows[security_id]["reported_return_pct"] == 12.5

    def test_resaving_same_date_and_source_replaces(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        for value in (100.0, 120.0):
            save_snapshot(
                conn,
                account_id=account_id,
                snapshot_date="2026-09-02",
                cash_eur=0.0,
                positions=[(security_id, 0.5, value, None)],
                source="revolut",
            )
        count = conn.execute("SELECT COUNT(*) AS n FROM portfolio_snapshots").fetchone()
        assert count["n"] == 1
        found = latest_snapshot(conn, account_id=account_id)
        assert found is not None
        assert found[0]["positions_value_eur"] == pytest.approx(120.0)

    def test_latest_returns_most_recent_date(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        for date, value in (("2026-09-02", 100.0), ("2026-09-09", 110.0)):
            save_snapshot(
                conn,
                account_id=account_id,
                snapshot_date=date,
                cash_eur=0.0,
                positions=[(security_id, 0.5, value, None)],
                source="revolut",
            )
        found = latest_snapshot(conn, account_id=account_id)
        assert found is not None
        assert found[0]["snapshot_date"] == "2026-09-09"

    def test_missing_snapshot_returns_none(
        self, conn: sqlite3.Connection, account_id: int
    ) -> None:
        assert latest_snapshot(conn, account_id=account_id) is None


class TestPrices:
    """Only the newest price per security is returned."""

    def test_latest_price_wins(
        self, conn: sqlite3.Connection, security_id: int
    ) -> None:
        conn.execute(
            "INSERT INTO prices VALUES (?, '2026-03-01', 60.0, 'USD', 'test', 'now')",
            (security_id,),
        )
        conn.execute(
            "INSERT INTO prices VALUES (?, '2026-03-05', 65.0, 'USD', 'test', 'now')",
            (security_id,),
        )
        assert latest_prices(conn)[security_id]["close_native"] == 65.0

    def test_empty_table_returns_empty_mapping(self, conn: sqlite3.Connection) -> None:
        assert latest_prices(conn) == {}


class TestOpeningDatabases:
    """Read commands must never conjure an empty portfolio."""

    def test_missing_database_raises(self, tmp_path) -> None:
        missing = tmp_path / "nope.db"
        with pytest.raises(FileNotFoundError, match="main.py init"):
            open_existing_db(missing)

    def test_missing_database_is_not_created(self, tmp_path) -> None:
        missing = tmp_path / "nope.db"
        with pytest.raises(FileNotFoundError):
            open_existing_db(missing)
        assert not missing.exists()

    def test_existing_database_opens_and_migrates(self, tmp_path) -> None:
        from store import open_db

        path = tmp_path / "portfolio.db"
        open_db(path).close()
        connection = open_existing_db(path)
        statuses = list_migrations(connection)
        assert all(status.status == "applied" for status in statuses)
        connection.close()


class TestDbStatusTables:
    """The status listing must reflect the schema, not a hardcoded roster."""

    def test_lists_tables_from_the_database(self, conn) -> None:
        from cmd_db import _table_names

        names = _table_names(conn)
        # Every table any migration created should appear without being named
        # anywhere in cmd_db.py.
        assert {"accounts", "trades", "events", "consensus_estimates"} <= set(names)

    def test_excludes_bookkeeping_tables(self, conn) -> None:
        from cmd_db import _table_names

        names = _table_names(conn)
        assert "schema_migrations" not in names
        assert not any(name.startswith("sqlite_") for name in names)
