"""Tests for the portfolio picture: facts computed from the book, never written."""

from __future__ import annotations

import sqlite3
from datetime import date
from types import SimpleNamespace

import pytest

import insights as insights_module
import portfolio as portfolio_module
from insights import _currency, portfolio_picture
from models import CashFlow, Thesis
from report import weekly_report
from seed import load_snapshot, seed_database
from store import insert_cash_flow
from store_research import save_thesis
from tests.test_seed import FIXTURE

TODAY = date(2026, 9, 7)


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    """AAA 110, BBB 90, CCC 150 and 5 cash: a EUR 355 book."""
    seed_database(conn, load_snapshot(FIXTURE))
    return conn


def _line(lines: list[str], start: str) -> str:
    return next(line for line in lines if line.startswith(start))


def _snapshot(conn: sqlite3.Connection, day: str, total: float, note: str = "") -> None:
    """Record a snapshot whose stated total differs from today's value.

    Positions are recorded as the fixture holds them: the fixture values its
    holdings from the newest snapshot, and one without positions would leave
    everything unpriced.
    """
    cursor = conn.execute(
        """INSERT INTO portfolio_snapshots (account_id, snapshot_date, total_value_eur,
        cash_eur, positions_value_eur, source, note, created_at)
        VALUES (1, ?, ?, 0, ?, 'weekly', ?, 'x')""",
        (day, total, total, note),
    )
    conn.executemany(
        "INSERT INTO snapshot_positions (snapshot_id, security_id, quantity, value_eur) "
        "VALUES (?, ?, ?, ?)",
        [
            (cursor.lastrowid, 1, 2.0, 110.0),
            (cursor.lastrowid, 2, 1.0, 90.0),
            (cursor.lastrowid, 3, 4.0, 150.0),
        ],
    )


class TestPositionsAndThemes:
    def test_the_largest_position_is_judged_against_the_limit(
        self, seeded: sqlite3.Connection
    ) -> None:
        line = _line(portfolio_picture(seeded, today=TODAY), "Largest")
        assert line == "Largest: CCC at 42.3%, over the 20% position limit."

    def test_under_the_limit_says_so(
        self, seeded: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(portfolio_module, "MAX_POSITION_WEIGHT_PCT", 50.0)
        monkeypatch.setattr(insights_module, "MAX_POSITION_WEIGHT_PCT", 50.0)
        line = _line(portfolio_picture(seeded, today=TODAY), "Largest")
        assert "under the 50% position limit" in line

    def test_the_heaviest_theme_spans_its_members(
        self, seeded: sqlite3.Connection
    ) -> None:
        # Sector labels would split this bet; the theme shows it whole.
        line = _line(portfolio_picture(seeded, today=TODAY), "Heaviest theme")
        assert line.startswith("Heaviest theme: shared, 56.3% across 2 holdings")
        assert "over the 40% alert level" in line

    def test_no_themes_means_no_theme_line(self, seeded: sqlite3.Connection) -> None:
        seeded.execute("DELETE FROM security_themes")
        lines = portfolio_picture(seeded, today=TODAY)
        assert not any(line.startswith("Heaviest theme") for line in lines)


class TestCurrency:
    @staticmethod
    def _held(currency: str, value: float) -> SimpleNamespace:
        security = SimpleNamespace(currency=currency)
        return SimpleNamespace(
            value_eur=value, position=SimpleNamespace(security=security)
        )

    def test_a_single_foreign_currency_is_named_as_a_second_bet(self) -> None:
        text = _currency([self._held("USD", 60), self._held("USD", 40)])
        assert text.startswith("Every holding is priced in USD")

    def test_a_mix_is_split_by_value(self) -> None:
        text = _currency([self._held("USD", 75), self._held("EUR", 25)])
        assert text.startswith("Priced in USD 75%, EUR 25%")


class TestReasons:
    def test_counts_only_moderate_or_better(self, seeded: sqlite3.Connection) -> None:
        save_thesis(
            seeded,
            Thesis(security_id=1, summary="a", source="user", conviction="moderate"),
        )
        save_thesis(
            seeded, Thesis(security_id=2, summary="b", source="user", conviction="weak")
        )
        seeded.execute("UPDATE thesis SET created_at = '2026-09-06T10:00:00+00:00'")
        line = _line(portfolio_picture(seeded, today=TODAY), "Reasons")
        # No thesis existed a week ago, so there is nothing to compare with.
        assert line == "Reasons you could defend: 1 of 3."

    def test_a_strengthened_reason_shows_as_progress(
        self, seeded: sqlite3.Connection
    ) -> None:
        for security_id, conviction in ((1, "moderate"), (2, "weak")):
            save_thesis(
                seeded,
                Thesis(
                    security_id=security_id,
                    summary="x",
                    source="user",
                    conviction=conviction,
                ),
            )
        seeded.execute("UPDATE thesis SET created_at = '2026-08-01T10:00:00+00:00'")
        save_thesis(
            seeded,
            Thesis(security_id=2, summary="y", source="user", conviction="moderate"),
        )
        seeded.execute(
            "UPDATE thesis SET created_at = '2026-09-06T10:00:00+00:00' "
            "WHERE security_id = 2 AND version = 2"
        )
        line = _line(portfolio_picture(seeded, today=TODAY), "Reasons")
        assert line == "Reasons you could defend: 2 of 3, up from 1 on Mon 31 Aug."


class TestSinceLastWeek:
    def test_money_added_is_not_a_gain(self, seeded: sqlite3.Connection) -> None:
        _snapshot(seeded, "2026-08-31", 300.0)
        insert_cash_flow(
            seeded,
            CashFlow(flow_date="2026-09-03", kind="CONTRIBUTION", amount_eur=20.0),
        )
        line = _line(portfolio_picture(seeded, today=TODAY), "Up")
        # 375 now, 300 then, 20 of it deposited: 55 of movement.
        assert line == "Up €55.00 since Mon 31 Aug, not counting €20.00 added."

    def test_a_snapshot_missing_a_holding_is_passed_over(
        self, seeded: sqlite3.Connection
    ) -> None:
        # Comparing against a total that left a holding out would report its
        # whole value as a gain.
        _snapshot(seeded, "2026-08-01", 355.0)
        _snapshot(
            seeded,
            "2026-08-31",
            200.0,
            note="Weekly snapshot. 1 holding(s) omitted for want of a price.",
        )
        lines = portfolio_picture(seeded, today=TODAY)
        assert "since Sat 1 Aug" in _line(lines, "Up")

    def test_a_young_benchmark_shows_no_amounts(
        self, seeded: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two euro figures after a few days read as a score.
        import benchmark

        young = benchmark.Comparison(
            ticker="IDX",
            name="Index",
            invested_eur=300.0,
            portfolio_eur=355.0,
            benchmark_eur=320.0,
            units=1.0,
            priced_on="2026-09-07",
            since="2026-09-02",
        )
        monkeypatch.setattr(benchmark, "compare", lambda *a, **k: young)
        line = _line(portfolio_picture(seeded, today=TODAY), "Benchmark")
        assert line.startswith("Benchmark (Index): recording, 5 of")
        assert "€" not in line

    def test_no_benchmark_history_means_no_benchmark_line(
        self, seeded: sqlite3.Connection
    ) -> None:
        lines = portfolio_picture(seeded, today=TODAY)
        assert not any(line.startswith("Same money") for line in lines)


class TestInTheReport:
    def test_the_summary_carries_the_picture(self, seeded: sqlite3.Connection) -> None:
        body = weekly_report(seeded, today=TODAY).body
        assert "<b>Your portfolio</b>" in body
        assert "• Largest: CCC at 42.3%" in body
