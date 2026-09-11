"""Tests for the process checks.

Each invariant is tested by breaking it. A check that has never been seen to
fail is indistinguishable from one that cannot fail, and the second is worse
than having no check at all.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from evals import observe, run_checks
from models import Thesis
from seed import load_snapshot, seed_database
from store_research import (
    create_llm_trace,
    create_research_run,
    log_llm_call,
    save_thesis,
)
from tests.test_seed import FIXTURE

TODAY = date(2026, 9, 7)


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_database(conn, load_snapshot(FIXTURE))
    return conn


def _check(conn: sqlite3.Connection, name: str):
    return next(check for check in run_checks(conn) if check.name == name)


class TestFalsifiability:
    """A thesis nothing can break cannot be tracked."""

    NAME = "every active thesis can be broken"

    def test_passes_with_breaking_conditions(self, seeded: sqlite3.Connection) -> None:
        save_thesis(
            seeded,
            Thesis(
                security_id=1,
                summary="x",
                source="user",
                what_would_break_it=("it stops being true",),
            ),
        )
        assert _check(seeded, self.NAME).passed is True

    def test_fails_without_them(self, seeded: sqlite3.Connection) -> None:
        save_thesis(seeded, Thesis(security_id=1, summary="x", source="user"))
        check = _check(seeded, self.NAME)
        assert check.passed is False
        assert check.offenders == ("AAA",)

    def test_superseded_versions_are_ignored(self, seeded: sqlite3.Connection) -> None:
        # Only what is believed now has to be falsifiable; history is history.
        save_thesis(seeded, Thesis(security_id=1, summary="old", source="user"))
        save_thesis(
            seeded,
            Thesis(
                security_id=1,
                summary="new",
                source="user",
                what_would_break_it=("something",),
            ),
        )
        assert _check(seeded, self.NAME).passed is True


class TestProvenance:
    """A citation that cannot be checked is worse than an honest absence."""

    NAME = "sourced claims carry a URL and a date"

    def _claim(self, conn: sqlite3.Connection, **kw) -> None:
        run_id = create_research_run(conn, run_date="2026-09-07", kind="deep")
        columns = ", ".join(kw)
        placeholders = ", ".join("?" * len(kw))
        conn.execute(
            f"INSERT INTO evidence (research_run_id, security_id, claim, "
            f"{columns}, created_at) VALUES (?, 1, 'a claim', {placeholders}, 'x')",
            (run_id, *kw.values()),
        )

    def test_passes_when_sourced_claims_are_complete(
        self, seeded: sqlite3.Connection
    ) -> None:
        self._claim(
            seeded,
            kind="sourced",
            source_url="https://e.com",
            published_date="2026-09-05",
        )
        assert _check(seeded, self.NAME).passed is True

    def test_background_claims_need_nothing(self, seeded: sqlite3.Connection) -> None:
        self._claim(seeded, kind="background")
        assert _check(seeded, self.NAME).passed is True

    def test_fails_on_a_sourced_claim_without_provenance(
        self, seeded: sqlite3.Connection
    ) -> None:
        # The database refuses this, so reaching it means something bypassed
        # the constraint — which is exactly what the check is for.
        seeded.execute("PRAGMA ignore_check_constraints = ON")
        self._claim(seeded, kind="sourced", published_date="2026-09-05")
        assert _check(seeded, self.NAME).passed is False


class TestWithdrawnAdvice:
    """An answer to a retired recommendation is not engagement."""

    NAME = "no decisions recorded on withdrawn advice"

    def _recommendation(self, conn: sqlite3.Connection, superseded: int | None) -> int:
        run_id = create_research_run(conn, run_date="2026-09-07", kind="deep")
        cursor = conn.execute(
            """
            INSERT INTO recommendation (run_date, research_run_id, security_id,
                                        action, rationale, urgency, expires_on,
                                        superseded_by_run_id, created_at)
            VALUES ('2026-09-07', ?, 1, 'REVIEW', 'because', 'low',
                    '2099-01-01', ?, 'x')
            """,
            (run_id, superseded),
        )
        return int(cursor.lastrowid)

    def test_passes_when_live_advice_is_answered(
        self, seeded: sqlite3.Connection
    ) -> None:
        rec = self._recommendation(seeded, superseded=None)
        seeded.execute(
            "INSERT INTO user_decision (recommendation_id, decision, decided_at) "
            "VALUES (?, 'approve', 'x')",
            (rec,),
        )
        assert _check(seeded, self.NAME).passed is True

    def test_fails_when_retired_advice_is_answered(
        self, seeded: sqlite3.Connection
    ) -> None:
        rec = self._recommendation(seeded, superseded=1)
        seeded.execute(
            "INSERT INTO user_decision (recommendation_id, decision, decided_at) "
            "VALUES (?, 'approve', 'x')",
            (rec,),
        )
        assert _check(seeded, self.NAME).passed is False


class TestResearchAuthority:
    """The pipeline must not edit the baseline it is measured against."""

    NAME = "research proposes and never adopts"

    def test_passes_when_the_owner_wrote_it(self, seeded: sqlite3.Connection) -> None:
        save_thesis(
            seeded,
            Thesis(
                security_id=1, summary="x", source="user", what_would_break_it=("y",)
            ),
        )
        assert _check(seeded, self.NAME).passed is True

    def test_fails_when_research_holds_the_active_thesis(
        self, seeded: sqlite3.Connection
    ) -> None:
        save_thesis(
            seeded,
            Thesis(
                security_id=1,
                summary="x",
                source="research",
                what_would_break_it=("y",),
            ),
        )
        check = _check(seeded, self.NAME)
        assert check.passed is False
        assert check.offenders == ("AAA",)


class TestZeroValuation:
    """Unpriceable must mean absent, never nothing."""

    NAME = "no holding recorded as worth nothing"

    def test_fails_on_a_zero_valued_position(self, seeded: sqlite3.Connection) -> None:
        from store import save_snapshot

        save_snapshot(
            seeded,
            account_id=1,
            snapshot_date="2026-09-07",
            cash_eur=0.0,
            positions=[(1, 2.0, 0.0, None)],
            source="weekly",
        )
        check = _check(seeded, self.NAME)
        assert check.passed is False
        assert check.offenders == ("2026-09-07 AAA",)

    def test_passes_when_a_holding_is_simply_absent(
        self, seeded: sqlite3.Connection
    ) -> None:
        from store import save_snapshot

        save_snapshot(
            seeded,
            account_id=1,
            snapshot_date="2026-09-07",
            cash_eur=0.0,
            positions=[(1, 2.0, 110.0, None)],
            source="weekly",
        )
        assert _check(seeded, self.NAME).passed is True


class TestStatedOdds:
    """A model's percentage is a guess in a lab coat."""

    NAME = "no stored advice quotes a probability or confidence"

    def _recommend(self, conn: sqlite3.Connection, rationale: str) -> None:
        run_id = create_research_run(conn, run_date="2026-09-07", kind="deep")
        conn.execute(
            """
            INSERT INTO recommendation (run_date, research_run_id, security_id,
                                        action, rationale, urgency, expires_on,
                                        created_at)
            VALUES ('2026-09-07', ?, 1, 'REVIEW', ?, 'low', '2099-01-01', 'x')
            """,
            (run_id, rationale),
        )

    def _assess(self, conn: sqlite3.Connection, answer: str) -> None:
        run_id = create_research_run(conn, run_date="2026-09-07", kind="deep")
        conn.execute(
            """
            INSERT INTO research_assessment (run_id, security_id, status, reason,
                questions_json, answers_json, triggered_json, open_questions_json,
                package_json, package_hash, coverage, created_at)
            VALUES (?, 1, 'unchanged', 'r', '["q"]', ?, '[]', '[]', '[]', 'h',
                    'insufficient', 'x')
            """,
            (
                run_id,
                f'[{{"question": "q", "answer": "{answer}", "kind": "background"}}]',
            ),
        )

    @pytest.mark.parametrize(
        "text",
        [
            "Revenue rose 12% and margins held at 40%.",
            "Roughly 35% of sales come from one customer.",
            "Keep 20% of the portfolio in cash.",
            "Consumer confidence fell 3% in August.",
        ],
    )
    def test_ordinary_figures_pass(self, seeded: sqlite3.Connection, text: str) -> None:
        self._recommend(seeded, text)
        self._assess(seeded, text)
        assert _check(seeded, self.NAME).passed is True

    @pytest.mark.parametrize(
        "text",
        [
            "There is a 70% chance the thesis holds.",
            "Confidence: 91%.",
            "I estimate the probability of a beat at 60 %.",
            "Odds of recovery are about 80%.",
        ],
    )
    def test_forecast_percentages_fail(
        self, seeded: sqlite3.Connection, text: str
    ) -> None:
        self._recommend(seeded, text)
        check = _check(seeded, self.NAME)
        assert check.passed is False
        assert check.offenders == ("recommendation 1",)

    def test_findings_are_checked_too(self, seeded: sqlite3.Connection) -> None:
        self._assess(seeded, "Likelihood of a dividend cut: 25%")
        check = _check(seeded, self.NAME)
        assert check.passed is False
        assert check.offenders[0].startswith("assessment ")


class TestByPromptVersion:
    """A prompt change should be visible as a different number, never a verdict."""

    def _named(self, conn: sqlite3.Connection, name: str):
        return next(
            (item for item in observe(conn, today=TODAY) if item.name == name), None
        )

    def _analyst_run(
        self, conn: sqlite3.Connection, version: str, kinds: list[str]
    ) -> None:
        trace = create_llm_trace(conn, operation="deep_research", feature="analyst")
        log_llm_call(
            conn,
            feature="analyst",
            model="m",
            requested_model="m",
            messages_json="[]",
            trace_id=trace,
            prompt_version=version,
        )
        run_id = create_research_run(
            conn, run_date="2026-09-07", kind="deep", trace_id=trace
        )
        for kind in kinds:
            conn.execute(
                "INSERT INTO evidence (research_run_id, security_id, claim, kind, "
                "source_url, published_date, created_at) VALUES (?, 1, 'c', ?, ?, ?, 'x')",
                (
                    run_id,
                    kind,
                    "https://e.com" if kind == "sourced" else None,
                    "2026-09-05" if kind == "sourced" else None,
                ),
            )

    def _decision_run(
        self,
        conn: sqlite3.Connection,
        version: str,
        actions: list[str],
        refused: list[str],
    ) -> None:
        trace = create_llm_trace(conn, operation="decision", feature="decision")
        log_llm_call(
            conn,
            feature="decision",
            model="m",
            requested_model="m",
            messages_json="[]",
            trace_id=trace,
            prompt_version=version,
        )
        run_id = create_research_run(
            conn, run_date="2026-09-07", kind="deep", trace_id=trace
        )
        for action in actions:
            conn.execute(
                "INSERT INTO recommendation (run_date, research_run_id, security_id, "
                "action, rationale, urgency, expires_on, created_at) "
                "VALUES ('2026-09-07', ?, 1, ?, 'r', 'low', '2099-01-01', 'x')",
                (run_id, action),
            )
        for action in refused:
            conn.execute(
                "INSERT INTO decision_refusal (run_id, ticker, action, rationale, "
                "refusal) VALUES (?, 'AAA', ?, 'r', 'no')",
                (run_id, action),
            )

    def test_absent_until_there_is_a_traced_call(
        self, seeded: sqlite3.Connection
    ) -> None:
        assert (
            self._named(seeded, "claims resting on a source, by analyst prompt") is None
        )
        assert (
            self._named(seeded, "proposals per decision run, by decide prompt") is None
        )

    def test_sourced_share_is_split_by_analyst_prompt(
        self, seeded: sqlite3.Connection
    ) -> None:
        self._analyst_run(seeded, "research_analyst/3", ["unanswered", "unanswered"])
        self._analyst_run(seeded, "research_analyst/4", ["sourced", "background"])
        self._analyst_run(seeded, "research_analyst/4", ["sourced"])
        item = self._named(seeded, "claims resting on a source, by analyst prompt")
        assert item is not None
        assert item.value == "research_analyst/3: 0/2 · research_analyst/4: 2/3"

    def test_proposals_count_refusals_and_split_by_decide_prompt(
        self, seeded: sqlite3.Connection
    ) -> None:
        self._decision_run(seeded, "decide/3", ["REVIEW"], [])
        self._decision_run(seeded, "decide/3", [], [])
        self._decision_run(seeded, "decide/4", ["ADD", "REVIEW"], ["EXIT"])
        item = self._named(seeded, "proposals per decision run, by decide prompt")
        assert item is not None
        assert item.value == (
            "decide/3: 2 runs, 0 trades, 1 REVIEW · decide/4: 1 runs, 2 trades, 1 REVIEW"
        )


class TestObservations:
    """Numbers with no correct value, reported without a verdict."""

    def _named(self, conn: sqlite3.Connection, name: str):
        return next(
            (item for item in observe(conn, today=TODAY) if item.name == name), None
        )

    def test_reports_when_there_is_nothing_yet(
        self, seeded: sqlite3.Connection
    ) -> None:
        item = self._named(seeded, "claims resting on a source")
        assert item is not None and "no claims" in item.value

    def test_counts_thin_theses_without_calling_them_defects(
        self, seeded: sqlite3.Connection
    ) -> None:
        save_thesis(
            seeded, Thesis(security_id=1, summary="x", source="user", conviction="weak")
        )
        save_thesis(
            seeded,
            Thesis(security_id=2, summary="y", source="user", conviction="strong"),
        )
        item = self._named(seeded, "holdings held on a thin reason")
        assert item is not None
        assert item.value == "1/2"
        assert "Not a defect" in item.note

    def test_never_researched_names_the_holdings(
        self, seeded: sqlite3.Connection
    ) -> None:
        item = self._named(seeded, "holdings never researched")
        assert item is not None
        assert "AAA" in item.value

    def test_no_observation_carries_a_verdict(self, seeded: sqlite3.Connection) -> None:
        # Reporting these as pass or fail would mean inventing a threshold
        # nobody can justify.
        for item in observe(seeded, today=TODAY):
            assert not hasattr(item, "passed")
