"""Tests for the chat agent's tools.

Two things are pinned here. What a model does wrong — a trailing semicolon, a
write dressed as a read, a limit it invented, a ticker it misremembered — and
what the rendered text says, because the text *is* the interface: a figure the
model misreads is wrong however correct the number behind it was.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from chat_tools import TOOL_NAMES, ToolResult, chat_tools, execute_tool
from models import Account, CashFlow, Security, Trade
from store import ensure_account, insert_cash_flow, insert_trade, upsert_security


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """A migrated on-disk database holding one priced and one unpriced holding."""
    from db.migrations import apply_migrations

    path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    ensure_account(
        conn,
        Account(name="revolut", broker="Revolut", currency="EUR", sync_mode="manual"),
    )
    priced = upsert_security(
        conn,
        Security(
            ticker="TEST",
            name="Test Corp",
            currency="USD",
            sector="Technology",
            feed_symbol="TEST",
        ),
    )
    dark = upsert_security(
        conn,
        Security(
            ticker="DARK",
            name="Unpriceable Ltd",
            currency="EUR",
            sector="Industrials",
            pricing_mode="manual",
        ),
    )
    insert_cash_flow(
        conn, CashFlow(flow_date="2026-01-01", kind="CONTRIBUTION", amount_eur=500.0)
    )
    for security_id, qty, eur in ((priced, 2.0, 100.0), (dark, 5.0, 50.0)):
        insert_trade(
            conn,
            Trade(
                security_id=security_id,
                trade_date="2026-01-02",
                side="BUY",
                quantity=qty,
                amount_eur=eur,
                fee_eur=1.0,
            ),
        )
    conn.execute(
        "INSERT INTO prices VALUES (?, '2026-09-01', 60.0, 'USD', 'test', 'now')",
        (priced,),
    )
    conn.execute(
        "INSERT INTO fx_rates VALUES ('2026-09-01', 'USD', 'EUR', 0.9, 'test', 'now')"
    )
    conn.commit()
    conn.close()
    return path


def _call(name: str, args: dict, db: Path) -> ToolResult:
    return execute_tool(name, args, db)


class TestDefinitions:
    """The schema the model is handed."""

    def test_every_defined_tool_is_dispatchable(self) -> None:
        # A tool the model can see but not call wastes a whole turn.
        for tool in chat_tools():
            assert tool["function"]["name"] in TOOL_NAMES

    def test_an_unknown_tool_names_the_real_ones(self, db: Path) -> None:
        result = _call("drop_everything", {}, db)
        assert result.ok is False
        assert "run_sql" in result.text


class TestRunSqlSafety:
    """The query arrives from a model and reaches SQLite."""

    @pytest.mark.parametrize(
        "query",
        [
            "DELETE FROM trades",
            "UPDATE trades SET amount_eur = 0",
            "INSERT INTO cash_flows VALUES (1,1,'2026-01-01','FEE',1,'x','now')",
            "DROP VIEW v_trades",
            "PRAGMA writable_schema = 1",
            "ATTACH DATABASE '/tmp/x.db' AS x",
        ],
    )
    def test_a_write_is_refused(self, db: Path, query: str) -> None:
        assert _call("run_sql", {"query": query}, db).ok is False

    def test_a_write_hidden_behind_with_still_fails(self, db: Path) -> None:
        # Past the keyword check because it begins WITH; the read-only
        # connection and the SELECT wrapper are what actually stop it.
        result = _call(
            "run_sql",
            {"query": "WITH x AS (SELECT 1) INSERT INTO cash_flows SELECT * FROM x"},
            db,
        )
        assert result.ok is False

    def test_a_write_attempt_changes_nothing(self, db: Path) -> None:
        before = _call("run_sql", {"query": "SELECT COUNT(*) AS n FROM trades"}, db)
        _call("run_sql", {"query": "DELETE FROM trades"}, db)
        after = _call("run_sql", {"query": "SELECT COUNT(*) AS n FROM trades"}, db)
        assert before.rows == after.rows == ({"n": 2},)

    @pytest.mark.parametrize("args", [{"query": "   "}, {}])
    def test_an_empty_query_is_refused(self, db: Path, args: dict) -> None:
        assert _call("run_sql", args, db).ok is False


class TestRunSqlRendering:
    """Rows reach the model as a table, not as repeated JSON keys."""

    def test_rows_render_as_a_markdown_table(self, db: Path) -> None:
        result = _call(
            "run_sql",
            {"query": "SELECT ticker, side, amount_eur FROM v_trades ORDER BY ticker"},
            db,
        )
        assert "| ticker | side | amount_eur |" in result.text
        assert "| DARK | BUY | 50 |" in result.text
        assert result.text.startswith("2 rows.")

    def test_structured_rows_come_back_alongside_the_text(self, db: Path) -> None:
        # A chart has to compute on the values, not parse the table back.
        result = _call("run_sql", {"query": "SELECT ticker FROM v_trades"}, db)
        assert result.rows == ({"ticker": "TEST"}, {"ticker": "DARK"})

    def test_sql_null_is_spelled_out(self, db: Path) -> None:
        # A blank cell would read as a rendering gap rather than as no value.
        result = _call("run_sql", {"query": "SELECT NULL AS missing"}, db)
        assert "| NULL |" in result.text

    def test_a_float_artefact_is_not_shown_as_precision(self, db: Path) -> None:
        result = _call("run_sql", {"query": "SELECT 151.0 / 3 AS third"}, db)
        assert "50.3333" in result.text
        assert "50.33333333" not in result.text

    def test_a_pipe_in_a_value_cannot_break_the_table(self, db: Path) -> None:
        result = _call("run_sql", {"query": "SELECT 'a|b' AS note"}, db)
        assert "a\\|b" in result.text

    def test_a_huge_cell_is_truncated(self, db: Path) -> None:
        # The row cap bounds rows, not their size: one llm_call.messages_json
        # would otherwise crowd out the conversation it belongs to.
        result = _call("run_sql", {"query": "SELECT printf('%.900d', 7) AS big"}, db)
        assert "truncated" in result.text
        assert len(result.text) < 600

    def test_no_rows_says_the_query_ran(self, db: Path) -> None:
        # Silence would be indistinguishable from a failure.
        result = _call("run_sql", {"query": "SELECT * FROM v_trades WHERE 0"}, db)
        assert result.ok is True
        assert "No rows" in result.text
        assert result.rows == ()

    def test_hitting_the_limit_is_announced(self, db: Path) -> None:
        # The model otherwise reads a truncated answer as a complete one.
        result = _call("run_sql", {"query": "SELECT * FROM v_trades", "limit": 1}, db)
        assert "there may be more" in result.text

    def test_the_row_cap_overrides_a_larger_request(self, db: Path) -> None:
        from config import CHAT_SQL_ROW_LIMIT

        result = _call(
            "run_sql", {"query": "SELECT * FROM v_trades", "limit": 10_000}, db
        )
        assert len(result.rows) <= CHAT_SQL_ROW_LIMIT

    def test_a_nonsense_limit_falls_back_to_the_default(self, db: Path) -> None:
        result = _call(
            "run_sql", {"query": "SELECT * FROM v_trades", "limit": "lots"}, db
        )
        assert len(result.rows) == 2

    def test_a_trailing_semicolon_does_not_break_the_wrapper(self, db: Path) -> None:
        # Every model writes one eventually, and the error it produced read like
        # a fault in the query rather than in the wrapping.
        result = _call("run_sql", {"query": "SELECT ticker FROM v_trades;"}, db)
        assert len(result.rows) == 2

    def test_a_broken_query_returns_its_message(self, db: Path) -> None:
        # The model has to read the failure and write a different query, so the
        # message matters more than the status.
        result = _call("run_sql", {"query": "SELECT * FROM no_such_view"}, db)
        assert result.ok is False
        assert "no_such_view" in result.text


class TestPortfolioSnapshot:
    """The authority for every euro figure the agent states."""

    def test_an_unpriced_holding_is_named_not_zeroed(self, db: Path) -> None:
        result = _call("portfolio_snapshot", {}, db)
        assert (
            "| DARK | 5 | €10.20 | €51.00 | UNPRICED | — | — | never |" in result.text
        )
        assert "UNPRICED: DARK" in result.text
        assert "Never treat an unpriced holding as zero" in result.text

    def test_the_total_is_described_as_a_floor(self, db: Path) -> None:
        # 500 contributed, 152 spent with fees, TEST worth 2 * 60 * 0.9 = 108.
        result = _call("portfolio_snapshot", {}, db)
        assert "Cash: €348.00." in result.text
        assert "Total: €456.00" in result.text
        assert "the total is a floor" in result.text

    def test_the_priced_row_carries_cost_value_return_and_weight(
        self, db: Path
    ) -> None:
        result = _call("portfolio_snapshot", {}, db)
        # 2 units, €101 basis including the fee, €108 value, +6.9%, 23.7% weight.
        assert (
            "| TEST | 2 | €50.50 | €101.00 | €108.00 | +6.9% | 23.7% |" in result.text
        )

    def test_the_weight_is_given_so_it_is_not_computed(self, db: Path) -> None:
        # Left out, the model divides by whatever denominator it picks.
        result = _call("portfolio_snapshot", {}, db)
        (priced,) = [row for row in result.rows if row["ticker"] == "TEST"]
        assert priced["weight_pct"] == pytest.approx(108.0 / 456.0 * 100.0)

    def test_a_single_ticker_is_reported_in_prose(self, db: Path) -> None:
        result = _call("portfolio_snapshot", {"ticker": "TEST"}, db)
        assert "TEST — Test Corp" in result.text
        assert "Sector: Technology" in result.text
        assert "quoted in USD" in result.text
        assert "2026-09-01 (test)" in result.text
        assert "|" not in result.text

    def test_a_single_unpriced_ticker_says_not_zero(self, db: Path) -> None:
        result = _call("portfolio_snapshot", {"ticker": "DARK"}, db)
        assert "Do not report it as zero" in result.text
        assert "has no weight" in result.text

    def test_one_ticker_still_reports_the_whole_total(self, db: Path) -> None:
        # A weight against a filtered total would be meaningless.
        result = _call("portfolio_snapshot", {"ticker": "TEST"}, db)
        assert "€456.00 total" in result.text

    def test_a_lowercase_ticker_is_accepted(self, db: Path) -> None:
        assert (
            "TEST — Test Corp"
            in _call("portfolio_snapshot", {"ticker": "test"}, db).text
        )

    def test_an_unheld_ticker_says_what_is_held(self, db: Path) -> None:
        # The model guesses tickers; listing the real ones saves a round-trip.
        result = _call("portfolio_snapshot", {"ticker": "NVDA"}, db)
        assert result.ok is False
        assert "NVDA" in result.text
        assert "TEST" in result.text

    def test_an_empty_portfolio_says_so(self, tmp_path: Path) -> None:
        from db.migrations import apply_migrations

        empty = tmp_path / "empty.db"
        conn = sqlite3.connect(empty)
        apply_migrations(conn)
        conn.commit()
        conn.close()
        result = _call("portfolio_snapshot", {}, empty)
        assert result.ok is True
        assert "No open positions" in result.text


class TestConcentrationReport:
    """Weights, with the limits they are judged against."""

    def test_weights_carry_the_configured_limits(self, db: Path) -> None:
        from config import CONCENTRATION_ALERT_PCT, MAX_POSITION_WEIGHT_PCT

        result = _call("concentration_report", {}, db)
        assert f"{MAX_POSITION_WEIGHT_PCT:.1f}%" in result.text
        assert f"{CONCENTRATION_ALERT_PCT:.1f}%" in result.text

    def test_an_unpriced_holding_is_declared_excluded(self, db: Path) -> None:
        result = _call("concentration_report", {}, db)
        assert "Excluded from every weight" in result.text
        assert "DARK" in result.text

    def test_grouping_by_sector_is_accepted(self, db: Path) -> None:
        result = _call("concentration_report", {"group_by": "sector"}, db)
        assert "Weights by sector" in result.text
        assert "| Technology |" in result.text

    def test_theme_grouping_explains_sums_past_100(self, db: Path) -> None:
        result = _call("concentration_report", {"group_by": "theme"}, db)
        assert "may sum past 100%" in result.text

    def test_an_unknown_grouping_is_refused(self, db: Path) -> None:
        assert _call("concentration_report", {"group_by": "mood"}, db).ok is False


class TestReadOnlyConnection:
    """The connection must not be able to create or change anything."""

    def test_the_connection_itself_refuses_a_write(self, db: Path) -> None:
        # The keyword filter only stops an obviously-wrong statement. This is
        # the check that actually makes the tool read-only, so it is tested
        # against the connection rather than through the filter.
        from chat_tools import _open_read_only

        conn = _open_read_only(db)
        try:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute("DELETE FROM trades")
        finally:
            conn.close()

    def test_a_runaway_query_times_out_instead_of_hanging(self, db: Path) -> None:
        # A recursive CTE with no termination is a plausible thing for a model
        # to write, and the person is watching a placeholder while it runs.
        import time

        started = time.monotonic()
        result = _call(
            "run_sql",
            {
                "query": (
                    "SELECT COUNT(*) AS n FROM ("
                    "WITH RECURSIVE r(n) AS ("
                    "SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT n FROM r)"
                )
            },
            db,
        )
        assert result.ok is False
        assert time.monotonic() - started < 15

    def test_a_database_without_the_views_says_to_migrate(self, tmp_path: Path) -> None:
        # A read-only connection cannot apply a migration, so this is the one
        # state the tools cannot fix themselves. SQLite's own "no such table:
        # v_cash_balance" reads like a bug in CashAsh rather than a database
        # that is simply behind.
        stale = tmp_path / "stale.db"
        conn = sqlite3.connect(stale)
        conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        for tool in ("portfolio_snapshot", "run_sql", "concentration_report"):
            args = {"query": "SELECT 1"} if tool == "run_sql" else {}
            assert "db migrate" in _call(tool, args, stale).text, tool

    def test_a_missing_database_is_an_error_not_a_new_file(
        self, tmp_path: Path
    ) -> None:
        # Only 'init' may create a database; a chat query must never be what
        # brings an empty one into existence.
        missing = tmp_path / "absent.db"
        assert _call("run_sql", {"query": "SELECT 1"}, missing).ok is False
        assert not missing.exists()
