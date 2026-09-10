"""Evidence coverage must not promote explanations into checked company facts."""

import json
import sqlite3

import pytest

from db.migrations import discover_migrations
from research_evidence import save_assessment, sufficient_coverage
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
]


class TestSourcedCoverage:
    @pytest.mark.parametrize("questions,findings,expected", CASES)
    def test_storage_and_upgrade_agree_on_coverage(
        self,
        book: sqlite3.Connection,
        questions: list[str],
        findings: list[tuple[str, str]],
        expected: bool,
    ) -> None:
        answers = [
            dict(question=q, answer="Synthetic finding", kind=kind)
            for q, kind in findings
        ]
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
        # Simulate a historical label from the former, less strict rule.
        book.execute(
            "UPDATE research_assessment SET coverage='sufficient' WHERE run_id=?",
            (run,),
        )
        migration = next(
            m for m in discover_migrations() if m.key.endswith("012_research_coverage")
        )
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
        migration = next(
            m for m in discover_migrations() if m.key.endswith("012_research_coverage")
        )
        migration.upgrade(book)
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
