"""Tests for the weekly run: order, and continuing past what fails."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

import weekly as weekly_module
from seed import load_snapshot, seed_database
from tests.test_seed import FIXTURE
from weekly import run_weekly

TODAY = date(2026, 9, 7)


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_database(conn, load_snapshot(FIXTURE))
    return conn


@pytest.fixture
def stages(monkeypatch: pytest.MonkeyPatch):
    """Replace every stage with a recorder, optionally made to fail."""

    def install(fail: set[str] | None = None, selected=None):
        failing = fail or set()
        called: list[str] = []

        def stage(name: str, result):  # type: ignore[no-untyped-def]
            def fake(*args, **kwargs):  # type: ignore[no-untyped-def]
                called.append(name)
                if name in failing:
                    raise RuntimeError(f"{name} exploded")
                return result

            return fake

        monkeypatch.setattr(
            weekly_module,
            "_sync",
            lambda conn, *, account_id: (
                called.append("sync"),
                weekly_module.StageResult("sync", "sync" not in failing, "done"),
            )[1],
        )
        import cmd_sync
        import decisions
        import research_coverage
        import research

        monkeypatch.setattr(
            research,
            "run_triage",
            stage(
                "triage",
                (
                    1,
                    [
                        {"ticker": t, "reason": "because", "selected": True}
                        for t in (selected if selected is not None else ["AAA"])
                    ],
                    "",
                ),
            ),
        )
        monkeypatch.setattr(research, "research_security", stage("research", None))
        monkeypatch.setattr(decisions, "run_decision", stage("decide", (2, [], "")))
        monkeypatch.setattr(cmd_sync, "sync_prices", lambda *a, **k: {})
        # Orchestration tests isolate selection; deterministic rotation has its own tests.
        monkeypatch.setattr(
            research_coverage,
            "select_research",
            lambda conn, selected, *, today, limit: (
                selected[:limit],
                [t for t, _ in selected[limit:]],
            ),
        )
        return called

    return install


class TestOrder:
    """Stages run in the order the pipeline depends on."""

    def test_every_stage_runs(self, seeded: sqlite3.Connection, stages) -> None:
        called = stages()
        outcome = run_weekly(seeded, today=TODAY)
        assert called[:3] == ["sync", "triage", "research"]
        assert [stage.name for stage in outcome.stages] == [
            "sync",
            "triage",
            "research",
            "decide",
            "report",
        ]

    def test_report_is_produced(self, seeded: sqlite3.Connection, stages) -> None:
        stages()
        assert run_weekly(seeded, today=TODAY).report is not None


class TestDegradation:
    """A partial answer beats none when the next attempt is a week away."""

    def test_a_failed_sync_does_not_stop_the_run(
        self, seeded: sqlite3.Connection, stages
    ) -> None:
        # Stale prices are usable and are marked stale; no prices is not a
        # reason to skip a week's thinking.
        called = stages(fail={"sync"})
        outcome = run_weekly(seeded, today=TODAY)
        assert "triage" in called
        assert outcome.report is not None

    def test_a_failed_triage_still_reaches_the_report(
        self, seeded: sqlite3.Connection, stages
    ) -> None:
        stages(fail={"triage"})
        outcome = run_weekly(seeded, today=TODAY)
        assert outcome.report is not None
        assert not outcome.ok

    def test_one_failed_holding_does_not_stop_the_others(
        self, seeded: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, stages
    ) -> None:
        stages(selected=["AAA", "BBB", "CCC"])
        import research

        attempted: list[str] = []

        def flaky(conn, *, ticker, trigger, today=None, **kwargs):  # type: ignore[no-untyped-def]
            attempted.append(ticker)
            if ticker == "BBB":
                raise RuntimeError("no thesis")

        monkeypatch.setattr(research, "research_security", flaky)
        outcome = run_weekly(seeded, today=TODAY)
        assert attempted == ["AAA", "BBB", "CCC"]
        assert outcome.researched == ["AAA", "CCC"]

    def test_a_failed_decision_still_reports(
        self, seeded: sqlite3.Connection, stages
    ) -> None:
        stages(fail={"decide"})
        outcome = run_weekly(seeded, today=TODAY)
        assert outcome.report is not None
        assert not outcome.ok

    def test_a_clean_run_is_ok(self, seeded: sqlite3.Connection, stages) -> None:
        stages()
        assert run_weekly(seeded, today=TODAY).ok is True


class TestResearchCap:
    """Depth is the expensive stage and the slow one."""

    def test_extra_selections_wait_a_week(
        self, seeded: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, stages
    ) -> None:
        stages(selected=["AAA", "BBB", "CCC"])
        import research

        attempted: list[str] = []
        monkeypatch.setattr(
            research,
            "research_security",
            lambda conn, *, ticker, **kw: attempted.append(ticker),
        )
        run_weekly(seeded, today=TODAY, max_research=2)
        assert attempted == ["AAA", "BBB"]

    def test_research_can_be_skipped_entirely(
        self, seeded: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, stages
    ) -> None:
        stages()
        import research

        monkeypatch.setattr(
            research,
            "research_security",
            lambda *a, **k: pytest.fail("research should not have run"),
        )
        outcome = run_weekly(seeded, today=TODAY, skip_research=True)
        stage = next(s for s in outcome.stages if s.name == "research")
        assert stage.skipped

    def test_nothing_selected_skips_research(
        self, seeded: sqlite3.Connection, stages
    ) -> None:
        stages(selected=[])
        outcome = run_weekly(seeded, today=TODAY)
        stage = next(s for s in outcome.stages if s.name == "research")
        assert stage.skipped
        assert outcome.researched == []
