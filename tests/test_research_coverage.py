"""Evidence coverage must not promote explanations into checked company facts."""

import json
import sqlite3

import pytest

from db.migrations import discover_migrations
from research_evidence import save_assessment, sufficient_coverage, uncovered_questions
from review_text import evidence_text
from store_research import active_thesis, create_research_run
from tests.test_review_workflow import TODAY, book as book, model
from decisions import run_decision


CASES = [
    (["Revenue?"], [("Revenue?", "sourced")], True),
    (["Revenue?", "Debt?"], [("Revenue?", "sourced"), ("Debt?", "background")], False),
    (["Revenue?", "Debt?"], [("Revenue?", "sourced")], False),
    (["Revenue?"], [("Revenue?", "unanswered")], False),
    (["Revenue?"], [("Other question", "sourced")], False),
    (["Revenue?"], [], False),
    ([], [("Revenue?", "sourced")], False),
    (["Revenue?"], [("  REVENUE?  ", "sourced")], True),
    # An extra remark beside fully sourced planned questions is not a gap.
    (["Revenue?"], [("Revenue?", "sourced"), ("Context", "background")], True),
    # One sourced answer covers a question even if another attempt did not.
    (["Revenue?"], [("Revenue?", "background"), ("Revenue?", "sourced")], True),
    # A sourced answer that failed citation validation covers nothing.
    (["Revenue?"], [("Revenue?", "sourced:invalid")], False),
]


def _migration(suffix: str):
    return next(m for m in discover_migrations() if m.key.endswith(suffix))


class TestSourcedCoverage:
    @pytest.mark.parametrize("questions,findings,expected", CASES)
    def test_storage_and_upgrade_agree_on_coverage(
        self,
        book: sqlite3.Connection,
        questions: list[str],
        findings: list[tuple[str, str]],
        expected: bool,
    ) -> None:
        answers = []
        for q, kind in findings:
            answer = dict(question=q, answer="Synthetic finding", kind=kind)
            if kind == "sourced:invalid":
                answer.update(kind="sourced", validation_error="invalid citation")
            answers.append(answer)
        assert sufficient_coverage(questions, answers) is expected
        thesis = active_thesis(book, security_id=1)
        run = create_research_run(book, run_date=TODAY.isoformat(), kind="deep")
        save_assessment(
            book,
            run_id=run,
            security_id=1,
            thesis_id=thesis.id,
            status="unchanged",
            reason="Synthetic assessment",
            questions=questions,
            answers=answers,
            triggered=(),
            open_questions=(),
            items=[],
        )
        expected_label = "sufficient" if expected else "insufficient"
        row = book.execute(
            "SELECT * FROM research_assessment WHERE run_id=?", (run,)
        ).fetchone()
        assert row["coverage"] == expected_label
        # Simulate a historical label written under an older rule, in
        # whichever direction disagrees with the current one.
        wrong = "insufficient" if expected else "sufficient"
        book.execute(
            "UPDATE research_assessment SET coverage=? WHERE run_id=?", (wrong, run)
        )
        migration = _migration("013_coverage_per_question")
        migration.upgrade(book)
        migration.upgrade(book)
        after = book.execute(
            "SELECT * FROM research_assessment WHERE run_id=?", (run,)
        ).fetchone()
        assert after["coverage"] == expected_label
        assert after["answers_json"] == row["answers_json"]
        assert after["package_hash"] == row["package_hash"]
        assert active_thesis(book, security_id=1).id == thesis.id

    def test_background_cannot_unlock_a_trade(
        self, book: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.test_review_workflow import assessment

        run = assessment(book)
        book.execute(
            "UPDATE research_assessment SET questions_json=?, answers_json=? WHERE run_id=?",
            (
                json.dumps(["Revenue?", "Debt?"]),
                json.dumps(
                    [
                        dict(question="Revenue?", answer="Persists", kind="sourced"),
                        dict(
                            question="Debt?",
                            answer="General explanation",
                            kind="background",
                        ),
                    ]
                ),
                run,
            ),
        )
        _migration("013_coverage_per_question").upgrade(book)
        model(
            monkeypatch,
            {
                "recommendations": [
                    dict(
                        ticker="TEST",
                        action="ADD",
                        amount_eur=50,
                        rationale="Synthetic case",
                    )
                ]
            },
        )
        _, recommendations, _ = run_decision(book, today=TODAY)
        assert recommendations[0]["refused"]
        assert "Fresh research" in recommendations[0]["refusal"]


class TestGapsAreActionable:
    def test_evidence_view_names_the_uncovered_questions(
        self, book: sqlite3.Connection
    ) -> None:
        from tests.test_review_workflow import assessment

        run = assessment(book)
        book.execute(
            "UPDATE research_assessment SET questions_json=?, answers_json=?, "
            "coverage='insufficient' WHERE run_id=?",
            (
                json.dumps(["Revenue?", "Net debt at the last balance sheet date?"]),
                json.dumps(
                    [
                        dict(question="Revenue?", answer="Persists", kind="sourced"),
                        dict(
                            question="Net debt at the last balance sheet date?",
                            answer="Usually modest for the sector",
                            kind="background",
                        ),
                    ]
                ),
                run,
            ),
        )
        text = evidence_text(book, "TEST")
        assert "Needs a sourced answer" in text
        assert "- Net debt at the last balance sheet date?" in text
        assert "- Revenue?" not in text
        assert "context/evidence.json" in text

    def test_covered_assessment_lists_no_gaps(self, book: sqlite3.Connection) -> None:
        from tests.test_review_workflow import assessment

        assessment(book)
        assert "Needs a sourced answer" not in evidence_text(book, "TEST")

    def test_uncovered_preserves_plan_order_and_text(self) -> None:
        questions = ["B?", "A?", "  ", "C?"]
        answers = [dict(question="a?", kind="sourced")]
        assert uncovered_questions(questions, answers) == ["B?", "C?"]
