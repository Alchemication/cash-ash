"""Tests for the deep research pass: planning, provenance, and proposal-only updates."""

from __future__ import annotations

import json
import sqlite3

import pytest

import research as research_module
from evidence import EvidenceItem
from llm import LLMResult
from models import Thesis
from research import research_security
from seed import load_snapshot, seed_database
from store_research import active_thesis, save_thesis, thesis_history
from tests.test_seed import FIXTURE


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    """A seeded database where AAA has a thesis with a breaking condition."""
    seed_database(conn, load_snapshot(FIXTURE))
    save_thesis(
        conn,
        Thesis(
            security_id=1,
            summary="I like the product",
            source="user",
            conviction="weak",
            what_would_break_it=("they stop making it",),
            open_questions=("what is it worth",),
        ),
    )
    return conn


class _Source:
    """An evidence source returning a fixed list."""

    name = "fake"

    def __init__(self, items: list[EvidenceItem] | None = None) -> None:
        self._items = items or []
        self.asked: list[str] = []

    def fetch(self, symbol: str, *, limit: int) -> list[EvidenceItem]:
        self.asked.append(symbol)
        return self._items[:limit]


def _plan(*questions: str) -> str:
    return json.dumps(
        {
            "questions": [
                {"question": q, "why": "because", "answerable_from": "a filing"}
                for q in (questions or ("Did anything change?",))
            ],
            "not_this_week": ["valuation"],
            "assumed_but_unverified": [],
        }
    )


def _analysis(**kw) -> str:
    base = {
        "answers": [
            {
                "question": "Did anything change?",
                "answer": "Nothing in the evidence reaches the thesis.",
                "kind": "unanswered",
                "source_url": None,
                "published_date": None,
            }
        ],
        "thesis_status": "unchanged",
        "status_reason": "No evidence touched the stated conditions.",
        "breaking_conditions_triggered": [],
        "new_open_questions": [],
        "proposed_summary": None,
    }
    base.update(kw)
    return json.dumps(base)


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch):
    """Replace call_llm with a two-stage script: plan, then analysis."""

    def install(plan_text: str, analysis_text: str):
        queue = [plan_text, analysis_text]
        seen: list[str] = []

        def fake(conn, *, messages, **kwargs):  # type: ignore[no-untyped-def]
            seen.append(messages[-1]["content"])
            return LLMResult(
                text=queue.pop(0) if queue else _analysis(),
                model="fake",
                requested_model="fake",
                llm_call_id=1,
            )

        monkeypatch.setattr(research_module, "call_llm", fake)
        return seen

    return install


class TestPreconditions:
    """What research refuses to run on."""

    def test_unknown_ticker(self, seeded: sqlite3.Connection, fake_llm) -> None:
        fake_llm(_plan(), _analysis())
        with pytest.raises(ValueError, match="Unknown ticker"):
            research_security(seeded, ticker="ZZZ", source=_Source())

    def test_holding_without_a_thesis(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        # Research compares evidence against a stated reason; with no reason
        # there is nothing to compare to.
        fake_llm(_plan(), _analysis())
        with pytest.raises(ValueError, match="No thesis for BBB"):
            research_security(seeded, ticker="BBB", source=_Source())

    def test_planner_returning_nothing(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        fake_llm(json.dumps({"questions": []}), _analysis())
        with pytest.raises(ValueError, match="no questions"):
            research_security(seeded, ticker="AAA", source=_Source())


class TestResearchPass:
    """What one pass produces."""

    def test_plan_questions_reach_the_analyst(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        seen = fake_llm(_plan("Are they still making it?"), _analysis())
        result = research_security(seeded, ticker="AAA", source=_Source())
        assert result.questions == ("Are they still making it?",)
        assert "Are they still making it?" in seen[1]

    def test_thesis_and_breaking_conditions_reach_the_analyst(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        seen = fake_llm(_plan(), _analysis())
        research_security(seeded, ticker="AAA", source=_Source())
        assert "I like the product" in seen[1]
        assert "they stop making it" in seen[1]

    def test_evidence_is_fetched_by_feed_symbol(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        # BBB carries feed_symbol BBB-X; give it a thesis so research proceeds.
        save_thesis(seeded, Thesis(security_id=2, summary="because", source="user"))
        fake_llm(_plan(), _analysis())
        source = _Source()
        research_security(seeded, ticker="BBB", source=source)
        assert source.asked == ["BBB-X"]

    def test_absent_evidence_is_stated_not_hidden(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        # An absence of evidence is not evidence that nothing happened.
        seen = fake_llm(_plan(), _analysis())
        research_security(seeded, ticker="AAA", source=_Source())
        assert "absence of evidence" in seen[1]

    def test_evidence_items_are_rendered_with_provenance(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        seen = fake_llm(_plan(), _analysis())
        research_security(
            seeded,
            ticker="AAA",
            source=_Source(
                [EvidenceItem("A thing", "https://e.com/a", "2026-09-05", "Wire")]
            ),
        )
        assert "https://e.com/a" in seen[1]
        assert "2026-09-05" in seen[1]

    def test_run_is_recorded(self, seeded: sqlite3.Connection, fake_llm) -> None:
        fake_llm(_plan(), _analysis())
        result = research_security(seeded, ticker="AAA", source=_Source())
        row = seeded.execute(
            "SELECT kind FROM research_run WHERE id = ?", (result.run_id,)
        ).fetchone()
        assert row["kind"] == "deep"

    def test_unusable_status_falls_back_to_unchanged(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        fake_llm(_plan(), _analysis(thesis_status="catastrophic"))
        result = research_security(seeded, ticker="AAA", source=_Source())
        assert result.thesis_status == "unchanged"


class TestProvenance:
    """Claims keep the provenance they can actually support."""

    def _claim(self, **kw) -> dict:
        base = {
            "question": "Did anything change?",
            "answer": "a claim",
            "kind": "sourced",
            "source_url": "https://e.com/a",
            "published_date": "2026-09-05",
            "source_id": "E1",
            "supporting_quote": "a claim",
        }
        base.update(kw)
        return base

    def test_sourced_claim_is_stored_with_its_source(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        fake_llm(_plan(), _analysis(answers=[self._claim()]))
        result = research_security(
            seeded,
            ticker="AAA",
            source=_Source(
                [
                    EvidenceItem(
                        title="a claim", url="https://e.com/a", published="2026-09-05"
                    )
                ]
            ),
        )
        assert (result.evidence_count, result.sourced_count) == (1, 1)
        row = seeded.execute("SELECT kind, source_url FROM evidence").fetchone()
        assert row["kind"] == "sourced"
        assert row["source_url"] == "https://e.com/a"

    def test_citation_not_in_package_is_unanswered(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        # The claim may still be true; what it may not do is carry provenance
        # it does not have.
        fake_llm(_plan(), _analysis(answers=[self._claim(source_url=None)]))
        result = research_security(seeded, ticker="AAA", source=_Source())
        assert (result.evidence_count, result.sourced_count) == (1, 0)
        assert (
            seeded.execute("SELECT kind FROM evidence").fetchone()["kind"]
            == "unanswered"
        )

    def test_sourced_without_date_is_demoted(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        fake_llm(_plan(), _analysis(answers=[self._claim(published_date=None)]))
        result = research_security(seeded, ticker="AAA", source=_Source())
        assert result.sourced_count == 0

    def test_invented_kind_is_rejected(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        fake_llm(_plan(), _analysis(answers=[self._claim(kind="vibes")]))
        with pytest.raises(ValueError, match="kind"):
            research_security(seeded, ticker="AAA", source=_Source())

    def test_background_claim_keeps_no_source(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        fake_llm(_plan(), _analysis(answers=[self._claim(kind="background")]))
        research_security(seeded, ticker="AAA", source=_Source())
        row = seeded.execute(
            "SELECT source_url, published_date FROM evidence"
        ).fetchone()
        assert row["source_url"] is None and row["published_date"] is None

    def test_empty_claims_are_rejected(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        fake_llm(_plan(), _analysis(answers=[self._claim(answer="  ")]))
        with pytest.raises(ValueError, match="blank"):
            research_security(seeded, ticker="AAA", source=_Source())


class TestProposalOnly:
    """Research proposes. It never changes what the owner believes."""

    def test_unchanged_proposes_nothing(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        fake_llm(_plan(), _analysis())
        result = research_security(
            seeded,
            ticker="AAA",
            source=_Source(
                [
                    EvidenceItem(
                        title="a claim", url="https://e.com/a", published="2026-09-05"
                    )
                ]
            ),
        )
        assert result.proposed_version is None
        assert len(thesis_history(seeded, security_id=1)) == 1

    def test_broken_creates_a_proposal(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        fake_llm(
            _plan(),
            _analysis(
                answers=[TestProvenance()._claim()],
                thesis_status="broken",
                breaking_conditions_triggered=["they stop making it"],
            ),
        )
        result = research_security(
            seeded,
            ticker="AAA",
            source=_Source(
                [
                    EvidenceItem(
                        title="a claim", url="https://e.com/a", published="2026-09-05"
                    )
                ]
            ),
        )
        assert result.proposed_version == 2

    def test_the_active_thesis_is_untouched(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        # The invariant the whole design rests on: a pipeline able to rewrite
        # the thesis would be editing the baseline it is measured against.
        before = active_thesis(seeded, security_id=1)
        fake_llm(
            _plan(),
            _analysis(answers=[TestProvenance()._claim()], thesis_status="broken"),
        )
        research_security(
            seeded,
            ticker="AAA",
            source=_Source(
                [
                    EvidenceItem(
                        title="a claim", url="https://e.com/a", published="2026-09-05"
                    )
                ]
            ),
        )
        after = active_thesis(seeded, security_id=1)
        assert after.id == before.id
        assert after.version == before.version
        assert after.summary == before.summary
        assert after.thesis_status == "unexamined"

    def test_proposal_is_marked_proposed_and_sourced_to_research(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        fake_llm(
            _plan(),
            _analysis(
                answers=[TestProvenance()._claim()], thesis_status="deteriorating"
            ),
        )
        research_security(
            seeded,
            ticker="AAA",
            source=_Source(
                [
                    EvidenceItem(
                        title="a claim", url="https://e.com/a", published="2026-09-05"
                    )
                ]
            ),
        )
        row = seeded.execute(
            "SELECT status, source, thesis_status FROM thesis WHERE version = 2"
        ).fetchone()
        assert row["status"] == "proposed"
        assert row["source"] == "research"
        assert row["thesis_status"] == "deteriorating"

    def test_proposal_records_which_run_made_it(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        fake_llm(
            _plan(),
            _analysis(
                answers=[TestProvenance()._claim()], thesis_status="deteriorating"
            ),
        )
        result = research_security(
            seeded,
            ticker="AAA",
            source=_Source(
                [
                    EvidenceItem(
                        title="a claim", url="https://e.com/a", published="2026-09-05"
                    )
                ]
            ),
        )
        row = seeded.execute(
            "SELECT research_run_id FROM thesis WHERE version = 2"
        ).fetchone()
        assert row["research_run_id"] == result.run_id

    def test_a_revised_summary_alone_proposes(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        fake_llm(
            _plan(),
            _analysis(
                answers=[TestProvenance()._claim()],
                proposed_summary="A different reason entirely",
            ),
        )
        result = research_security(
            seeded,
            ticker="AAA",
            source=_Source(
                [
                    EvidenceItem(
                        title="a claim", url="https://e.com/a", published="2026-09-05"
                    )
                ]
            ),
        )
        assert result.proposed_version == 2
        row = seeded.execute("SELECT summary FROM thesis WHERE version = 2").fetchone()
        assert row["summary"] == "A different reason entirely"

    def test_rewording_is_not_required_to_propose(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        fake_llm(
            _plan(),
            _analysis(answers=[TestProvenance()._claim()], thesis_status="improving"),
        )
        research_security(
            seeded,
            ticker="AAA",
            source=_Source(
                [
                    EvidenceItem(
                        title="a claim", url="https://e.com/a", published="2026-09-05"
                    )
                ]
            ),
        )
        row = seeded.execute("SELECT summary FROM thesis WHERE version = 2").fetchone()
        assert row["summary"] == "I like the product"
