"""Tests for the no-store sandbox: what it discards and what it keeps."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from models import Security
from sandbox import run_routing, sandbox_cost, sandboxed
from store import connect_db, upsert_security
from store_research import create_llm_trace, create_research_run, log_llm_call


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """A migrated file database holding one security."""
    path = tmp_path / "portfolio.db"
    conn = connect_db(path, migrate=True)
    upsert_security(conn, Security(ticker="TEST", name="Test Corp"))
    conn.close()
    return path


def _real(path: Path) -> sqlite3.Connection:
    """Open the real database for assertions, without migrating it."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _log_one(conn: sqlite3.Connection, *, trace_id: int, cost: float = 0.25) -> int:
    """Record one model call."""
    return log_llm_call(
        conn,
        feature="analyst",
        model="fake/flash",
        requested_model="fake/flash",
        messages_json='[{"role":"user","content":"hello"}]',
        trace_id=trace_id,
        response_text="an answer",
        cost_usd=cost,
        input_tokens=10,
        output_tokens=20,
    )


class TestIsolation:
    """Nothing a sandbox run writes reaches the real database."""

    def test_rows_written_in_the_sandbox_are_discarded(self, db: Path) -> None:
        with sandboxed(db) as conn:
            create_research_run(conn, run_date="2026-09-12", kind="deep")
            assert conn.execute("SELECT COUNT(*) FROM research_run").fetchone()[0] == 1

        real = _real(db)
        assert real.execute("SELECT COUNT(*) FROM research_run").fetchone()[0] == 0
        real.close()

    def test_the_sandbox_sees_what_the_real_database_holds(self, db: Path) -> None:
        with sandboxed(db) as conn:
            row = conn.execute("SELECT ticker FROM securities").fetchone()
        assert row["ticker"] == "TEST"

    def test_a_missing_database_is_refused_rather_than_created(
        self, tmp_path: Path
    ) -> None:
        missing = tmp_path / "nothing.db"
        with pytest.raises(FileNotFoundError):
            with sandboxed(missing):
                pass
        assert not missing.exists()


class TestModelLog:
    """Calls cost real money, so their record survives the sandbox."""

    def test_calls_and_traces_are_copied_back(self, db: Path) -> None:
        with sandboxed(db) as conn:
            trace = create_llm_trace(conn, operation="deep_research", reference="TEST")
            _log_one(conn, trace_id=trace)

        real = _real(db)
        call = real.execute("SELECT * FROM llm_call").fetchone()
        assert call["feature"] == "analyst"
        assert call["model"] == "fake/flash"
        assert call["cost_usd"] == 0.25
        assert call["messages_json"] == '[{"role":"user","content":"hello"}]'
        assert call["input_tokens"] == 10

        kept = real.execute(
            "SELECT operation, reference FROM llm_trace WHERE id = ?",
            (call["trace_id"],),
        ).fetchone()
        assert kept["operation"] == "deep_research"
        assert kept["reference"] == "TEST"
        real.close()

    def test_a_trace_that_predates_the_sandbox_keeps_its_id(self, db: Path) -> None:
        existing = connect_db(db, migrate=False)
        trace = create_llm_trace(existing, operation="weekly_run")
        existing.close()

        with sandboxed(db) as conn:
            _log_one(conn, trace_id=trace)

        real = _real(db)
        assert real.execute("SELECT trace_id FROM llm_call").fetchone()[0] == trace
        assert real.execute("SELECT COUNT(*) FROM llm_trace").fetchone()[0] == 1
        real.close()

    def test_a_failed_run_still_records_what_it_spent(self, db: Path) -> None:
        # The record matters most when the run fell over: the money is gone
        # either way, and nothing else will show where it went.
        with pytest.raises(ValueError):
            with sandboxed(db) as conn:
                trace = create_llm_trace(conn, operation="deep_research")
                _log_one(conn, trace_id=trace)
                raise ValueError("the analyst returned nonsense")

        real = _real(db)
        assert real.execute("SELECT COUNT(*) FROM llm_call").fetchone()[0] == 1
        real.close()

    def test_a_run_making_no_calls_writes_nothing(self, db: Path) -> None:
        with sandboxed(db) as conn:
            conn.execute("SELECT 1")

        real = _real(db)
        assert real.execute("SELECT COUNT(*) FROM llm_call").fetchone()[0] == 0
        assert real.execute("SELECT COUNT(*) FROM llm_trace").fetchone()[0] == 0
        real.close()

    def test_two_runs_accumulate_rather_than_collide(self, db: Path) -> None:
        for _ in range(2):
            with sandboxed(db) as conn:
                trace = create_llm_trace(conn, operation="deep_research")
                _log_one(conn, trace_id=trace)

        real = _real(db)
        assert real.execute("SELECT COUNT(*) FROM llm_call").fetchone()[0] == 2
        traces = [row[0] for row in real.execute("SELECT id FROM llm_trace")]
        assert len(set(traces)) == 2
        real.close()


class TestCost:
    """What a sandbox run reports spending."""

    def test_cost_totals_one_trace(self, db: Path) -> None:
        with sandboxed(db) as conn:
            trace = create_llm_trace(conn, operation="deep_research")
            _log_one(conn, trace_id=trace, cost=0.1)
            _log_one(conn, trace_id=trace, cost=0.2)
            other = create_llm_trace(conn, operation="decision")
            _log_one(conn, trace_id=other, cost=5.0)
            assert sandbox_cost(conn, trace_id=trace) == (2, pytest.approx(0.3))

    def test_an_operation_without_a_trace_reports_nothing(self, db: Path) -> None:
        with sandboxed(db) as conn:
            assert sandbox_cost(conn, trace_id=None) == (0, 0.0)

    def test_calls_without_a_known_cost_do_not_break_the_total(self, db: Path) -> None:
        with sandboxed(db) as conn:
            trace = create_llm_trace(conn, operation="deep_research")
            log_llm_call(
                conn,
                feature="plan",
                model="fake/flash",
                requested_model="fake/flash",
                messages_json="[]",
                trace_id=trace,
                cost_usd=None,
            )
            assert sandbox_cost(conn, trace_id=trace) == (1, 0.0)


class TestRouting:
    """A one-run model override is allowed only where nothing is kept."""

    def test_no_override_is_no_routing(self) -> None:
        assert run_routing(None, no_store=True) is None
        assert run_routing(None, no_store=False) is None

    def test_an_override_needs_the_sandbox(self) -> None:
        with pytest.raises(ValueError, match="--no-store"):
            run_routing("analyst=some/model", no_store=False)

    def test_an_override_is_parsed_for_a_sandbox_run(self) -> None:
        assert run_routing("analyst=some/model,plan=other/model", no_store=True) == {
            "analyst": "some/model",
            "plan": "other/model",
        }

    def test_an_unknown_feature_is_refused(self) -> None:
        with pytest.raises(ValueError, match="Unknown feature"):
            run_routing("analysts=some/model", no_store=True)

    def test_a_malformed_pair_is_refused(self) -> None:
        with pytest.raises(ValueError, match="feature=model"):
            run_routing("analyst", no_store=True)

    def test_an_empty_override_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            run_routing(",", no_store=True)
