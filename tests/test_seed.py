"""Tests for snapshot loading, cost-basis derivation and seeding."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from portfolio import cash_eur, holdings, positions, total_value
from seed import load_snapshot, reconcile, seed_database

FIXTURE = Path(__file__).parent / "fixtures" / "seed_snapshot.toml"


@pytest.fixture
def snapshot():
    """The synthetic snapshot fixture."""
    return load_snapshot(FIXTURE)


class TestLoadSnapshot:
    """Parsing and validation of the snapshot file."""

    def test_reads_header_fields(self, snapshot) -> None:
        assert snapshot.date == "2026-01-15"
        assert snapshot.source == "testbroker"
        assert snapshot.cash_eur == 5.00
        assert snapshot.reported_total_eur == 355.00

    def test_reads_every_position(self, snapshot) -> None:
        assert [position.ticker for position in snapshot.positions] == [
            "AAA",
            "BBB",
            "CCC",
        ]

    def test_reads_optional_fields(self, snapshot) -> None:
        by_ticker = {position.ticker: position for position in snapshot.positions}
        assert by_ticker["BBB"].feed_symbol == "BBB-X"
        assert by_ticker["AAA"].feed_symbol is None
        assert by_ticker["AAA"].themes == ("alpha", "shared")
        assert by_ticker["AAA"].pricing_mode == "feed"

    def test_account_defaults(self, snapshot) -> None:
        assert snapshot.account.name == "revolut"
        assert snapshot.account.sync_mode == "manual"

    def test_missing_file_names_the_example(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="seed_snapshot.example.toml"):
            load_snapshot(tmp_path / "absent.toml")

    def test_missing_required_key_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "s.toml"
        path.write_text('date = "2026-01-01"\n[[position]]\nticker="A"\n')
        with pytest.raises(ValueError, match="missing required key 'cash_eur'"):
            load_snapshot(path)

    def test_no_positions_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "s.toml"
        path.write_text('date = "2026-01-01"\ncash_eur = 0.0\n')
        with pytest.raises(ValueError, match="no \\[\\[position\\]\\] entries"):
            load_snapshot(path)

    def test_incomplete_position_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "s.toml"
        path.write_text(
            'date = "2026-01-01"\ncash_eur = 0.0\n[[position]]\nticker = "A"\n'
        )
        with pytest.raises(ValueError, match="missing name, quantity, value_eur"):
            load_snapshot(path)

    def test_non_positive_quantity_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "s.toml"
        path.write_text(
            'date = "2026-01-01"\ncash_eur = 0.0\n[[position]]\n'
            'ticker = "A"\nname = "A"\nquantity = 0.0\nvalue_eur = 10.0\n'
        )
        with pytest.raises(ValueError, match="non-positive quantity"):
            load_snapshot(path)

    def test_duplicate_ticker_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "s.toml"
        entry = (
            '[[position]]\nticker = "A"\nname = "A"\nquantity = 1.0\nvalue_eur = 10.0\n'
        )
        path.write_text(f'date = "2026-01-01"\ncash_eur = 0.0\n{entry}{entry}')
        with pytest.raises(ValueError, match="duplicate tickers"):
            load_snapshot(path)


class TestReconciliation:
    """The derived book must agree with what the broker displayed."""

    def test_totals(self, snapshot) -> None:
        check = reconcile(snapshot)
        assert check["positions_value_eur"] == pytest.approx(350.00)
        assert check["total_eur"] == pytest.approx(355.00)
        assert check["total_difference_eur"] == pytest.approx(0.0)

    def test_cost_basis_is_derived_from_the_reported_return(self, snapshot) -> None:
        # Each fixture position is constructed to have cost exactly EUR 100.
        for position in snapshot.positions:
            assert position.cost_basis_eur == pytest.approx(100.0)
        assert reconcile(snapshot)["cost_basis_eur"] == pytest.approx(300.0)

    def test_implied_gain(self, snapshot) -> None:
        check = reconcile(snapshot)
        assert check["unrealised_gain_eur"] == pytest.approx(50.0)
        assert check["unrealised_return_pct"] == pytest.approx(50 / 300 * 100)

    def test_winners_cost_less_than_they_are_worth(self, snapshot) -> None:
        for position in snapshot.positions:
            if position.reported_return_pct > 0:
                assert position.cost_basis_eur < position.value_eur
            elif position.reported_return_pct < 0:
                assert position.cost_basis_eur > position.value_eur

    def test_absent_reported_total_yields_no_difference(self, tmp_path: Path) -> None:
        path = tmp_path / "s.toml"
        path.write_text(
            'date = "2026-01-01"\ncash_eur = 0.0\n[[position]]\n'
            'ticker = "A"\nname = "A"\nquantity = 1.0\nvalue_eur = 10.0\n'
        )
        check = reconcile(load_snapshot(path))
        assert check["reported_total_eur"] is None
        assert check["total_difference_eur"] is None


class TestSeedDatabase:
    """Seeding writes a ledger that reproduces the snapshot."""

    def test_creates_every_position(self, conn: sqlite3.Connection, snapshot) -> None:
        account = seed_database(conn, snapshot)
        assert len(positions(conn, account_id=account)) == len(snapshot.positions)

    def test_cash_matches_snapshot(self, conn: sqlite3.Connection, snapshot) -> None:
        account = seed_database(conn, snapshot)
        assert cash_eur(conn, account_id=account) == pytest.approx(snapshot.cash_eur)

    def test_total_matches_reported_total(
        self, conn: sqlite3.Connection, snapshot
    ) -> None:
        account = seed_database(conn, snapshot)
        rows = holdings(conn, account_id=account)
        cash = cash_eur(conn, account_id=account)
        assert total_value(rows, cash=cash) == pytest.approx(
            snapshot.reported_total_eur
        )

    def test_opening_trades_are_flagged_synthetic(
        self, conn: sqlite3.Connection, snapshot
    ) -> None:
        seed_database(conn, snapshot)
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE is_synthetic = 0"
        ).fetchone()
        assert row["n"] == 0

    def test_is_idempotent(self, conn: sqlite3.Connection, snapshot) -> None:
        account = seed_database(conn, snapshot)
        before = cash_eur(conn, account_id=account)
        seed_database(conn, snapshot)
        seed_database(conn, snapshot)
        assert len(positions(conn, account_id=account)) == len(snapshot.positions)
        assert cash_eur(conn, account_id=account) == pytest.approx(before)
        assert (
            conn.execute("SELECT COUNT(*) AS n FROM portfolio_snapshots").fetchone()[
                "n"
            ]
            == 1
        )

    def test_reseeding_corrects_classification_in_place(
        self, conn: sqlite3.Connection, snapshot, tmp_path: Path
    ) -> None:
        # The real reason seeding is idempotent: a mislabelled security must be
        # fixable by editing the snapshot and re-running, not by rebuilding.
        seed_database(conn, snapshot)
        edited = FIXTURE.read_text().replace(
            'themes = ["alpha", "shared"]', 'themes = ["corrected"]'
        )
        path = tmp_path / "edited.toml"
        path.write_text(edited)
        seed_database(conn, load_snapshot(path))
        themes = [
            row["theme"]
            for row in conn.execute(
                "SELECT theme FROM security_themes st "
                "JOIN securities s ON s.id = st.security_id WHERE s.ticker = 'AAA'"
            )
        ]
        assert themes == ["corrected"]
        assert conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"] == 3

    def test_feed_symbol_is_stored(self, conn: sqlite3.Connection, snapshot) -> None:
        seed_database(conn, snapshot)
        row = conn.execute(
            "SELECT feed_symbol FROM securities WHERE ticker = 'BBB'"
        ).fetchone()
        assert row["feed_symbol"] == "BBB-X"
