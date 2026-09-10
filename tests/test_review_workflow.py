"""Regressions for funded decisions, research provenance and owner follow-through."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

import decisions
from decisions import build_guardrail_context, run_decision
from evidence import EvidenceItem
from guardrails import GuardrailContext, check_proposal
from llm import LLMResult
from models import CashFlow, Security, Thesis, Trade
from portfolio import cash_eur, positions
from profiles import Profile
from report import _pending_recommendations, weekly_report
from research import _propose_thesis
from research_coverage import select_research
from research_evidence import gather_evidence, save_assessment, validate_answers
from store import (
    insert_cash_flow,
    insert_trade,
    save_fx_rate,
    save_prices,
    upsert_security,
)
from store_research import active_thesis, create_research_run, save_thesis
from store_workflow import finish_cycle, review_thesis, start_cycle
from workflow import capital_committed, record_execution, record_response

TODAY = date(2026, 9, 7)


@pytest.fixture
def book(conn: sqlite3.Connection, account_id: int) -> sqlite3.Connection:
    """EUR book with a fresh small holding and cash already inside total wealth."""
    security = upsert_security(
        conn, Security(ticker="TEST", name="Synthetic issuer", currency="EUR")
    )
    insert_cash_flow(
        conn,
        CashFlow(flow_date=TODAY.isoformat(), kind="CONTRIBUTION", amount_eur=1000),
    )
    insert_trade(
        conn,
        Trade(
            security_id=security,
            trade_date=TODAY.isoformat(),
            side="BUY",
            quantity=10,
            amount_eur=100,
        ),
    )
    save_prices(conn, [(security, TODAY.isoformat(), 10, "EUR", "test")])
    save_thesis(
        conn,
        Thesis(
            security_id=security,
            summary="Recurring revenue persists",
            source="user",
            what_would_break_it=("Revenue contracts",),
        ),
    )
    return conn


def assessment(
    conn: sqlite3.Connection,
    *,
    ticker: str = "TEST",
    day: date = TODAY,
    coverage: bool = True,
) -> int:
    """Store a completed assessment without invoking a provider."""
    security = conn.execute(
        "SELECT id FROM securities WHERE ticker=?", (ticker,)
    ).fetchone()[0]
    thesis = active_thesis(conn, security_id=security)
    run = create_research_run(conn, run_date=day.isoformat(), kind="deep")
    save_assessment(
        conn,
        run_id=run,
        security_id=security,
        thesis_id=thesis.id,
        status="unchanged",
        reason="Revenue observation",
        questions=["Revenue?"],
        answers=[
            dict(
                question="Revenue?",
                answer="Revenue persists",
                kind="sourced" if coverage else "unanswered",
                source_url="https://example.com/revenue",
                published_date=day.isoformat(),
            )
        ],
        triggered=(),
        open_questions=(),
        items=[],
    )
    return run


def recommendation(
    conn: sqlite3.Connection,
    *,
    action: str = "ADD",
    amount: float | None = 50,
    ticker: str = "TEST",
    expires: str = "2026-09-14",
) -> int:
    """Create an independent recommendation for workflow tests."""
    security = conn.execute(
        "SELECT id FROM securities WHERE ticker=?", (ticker,)
    ).fetchone()[0]
    with conn:
        cursor = conn.execute(
            """INSERT INTO recommendation
            (run_date,security_id,action,amount_eur,rationale,expires_on,created_at)
            VALUES (?,?,?,?,?,?,?)""",
            (
                TODAY.isoformat(),
                security,
                action,
                amount,
                "Synthetic reason",
                expires,
                TODAY.isoformat(),
            ),
        )
    return cursor.lastrowid


def model(monkeypatch: pytest.MonkeyPatch, payload: dict) -> list[str]:
    """Capture the decision input while returning a fixed proposal batch."""
    messages: list[str] = []

    def reply(
        conn: sqlite3.Connection, *, messages: list[dict], **kwargs: object
    ) -> LLMResult:
        captured.extend(m["content"] for m in messages)
        return LLMResult(text=json.dumps(payload), model="test", requested_model="test")

    captured = messages
    monkeypatch.setattr(decisions, "call_llm", reply)
    return messages


class TestFundedWorkflow:
    def test_approval_reserves_and_execution_updates_ledger(
        self, book: sqlite3.Connection
    ) -> None:
        rec = recommendation(book)
        before = cash_eur(book)
        record_response(book, rec, "approve", today=TODAY)
        assert capital_committed(book, TODAY)[:2] == (50, 50)
        assert (
            build_guardrail_context(book, today=TODAY).available_capital_eur
            == before - 50
        )
        record_execution(book, rec, quantity=5, amount=50, fee=1, day=TODAY.isoformat())
        assert cash_eur(book) == before - 51
        assert positions(book)[0].quantity == 15
        assert positions(book)[0].cost_basis_eur == 151
        assert capital_committed(book, TODAY)[:2] == (0, 50)
        assert book.execute("SELECT trade_id FROM execution").fetchone()[0] is not None

    def test_no_double_execution(self, book: sqlite3.Connection) -> None:
        rec = recommendation(book)
        record_response(book, rec, "approve", today=TODAY)
        record_execution(book, rec, quantity=5, amount=50, day=TODAY.isoformat())
        with pytest.raises(ValueError, match="already recorded"):
            record_execution(book, rec, quantity=5, amount=50, day=TODAY.isoformat())
        assert positions(book)[0].quantity == 15

    def test_repeated_approval_is_idempotent(self, book: sqlite3.Connection) -> None:
        rec = recommendation(book)
        record_response(book, rec, "approve", today=TODAY)
        record_response(book, rec, "approve", today=TODAY)
        assert book.execute("SELECT COUNT(*) FROM user_decision").fetchone()[0] == 1

    def test_cancelled_execution_releases_reservation(
        self, book: sqlite3.Connection
    ) -> None:
        rec = recommendation(book)
        record_response(book, rec, "approve", today=TODAY)
        record_execution(
            book, rec, skipped=True, day=TODAY.isoformat(), note="Changed my mind"
        )
        assert capital_committed(book, TODAY)[:2] == (0, 0)
        assert positions(book)[0].quantity == 10

    def test_stale_approval_rechecks_price(self, book: sqlite3.Connection) -> None:
        rec = recommendation(book, expires="2026-10-01")
        with pytest.raises(ValueError, match="changed"):
            record_response(book, rec, "approve", today=TODAY + timedelta(days=10))
        assert capital_committed(book, TODAY)[:2] == (0, 0)

    def test_planned_money_never_funds_a_trade(self) -> None:
        context = GuardrailContext(
            total_value_eur=1000, cash_eur=0, monthly_contribution_eur=500
        )
        assert check_proposal(
            action="BUY", ticker="NEW", amount_eur=50, context=context
        ).refused

    def test_oversell_rejected_before_ledger_mutation(
        self, book: sqlite3.Connection
    ) -> None:
        rec = recommendation(book, action="EXIT", amount=100)
        # Owner response is fixture setup; the test isolates actual fill validation.
        book.execute(
            "INSERT INTO user_decision(recommendation_id,decision,decided_at) VALUES (?,'approve',?)",
            (rec, TODAY.isoformat()),
        )
        with pytest.raises(ValueError, match="exceeds"):
            record_execution(book, rec, quantity=11, amount=110, day=TODAY.isoformat())
        assert positions(book)[0].quantity == 10
        assert book.execute("SELECT COUNT(*) FROM execution").fetchone()[0] == 0

    @pytest.mark.parametrize("amount", [float("nan"), float("inf"), -1])
    def test_bad_amounts_blocked(self, amount: float) -> None:
        context = GuardrailContext(
            total_value_eur=1000, cash_eur=100, monthly_contribution_eur=0
        )
        assert check_proposal(
            action="BUY", ticker="NEW", amount_eur=amount, context=context
        ).refused

    def test_sale_cannot_exceed_holding(self) -> None:
        context = GuardrailContext(
            total_value_eur=1000,
            cash_eur=100,
            monthly_contribution_eur=0,
            values_by_ticker={"TEST": 30},
            weights_by_ticker={"TEST": 3},
            thesis_status_by_ticker={"TEST": "broken"},
        )
        assert (
            check_proposal(
                action="TRIM", ticker="TEST", amount_eur=10000, context=context
            ).amount_eur
            == 30
        )

    def test_expired_approval_can_be_cancelled(self, book: sqlite3.Connection) -> None:
        rec = recommendation(book)
        record_response(book, rec, "approve", today=TODAY)
        record_response(book, rec, "reject", today=TODAY + timedelta(days=20))
        assert capital_committed(book, TODAY)[0] == 0


class TestAtomicDecisions:
    def test_reads_current_owner_context_and_excludes_unrequested_files(
        self, book: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        profile = Profile(name="test", telegram_id=1, root=tmp_path)
        profile.context.mkdir()
        profile.context_path("strategy.md").write_text(
            "Keep funds available for tuition."
        )
        profile.context_path("investor.md").write_text("My horizon is five years.")
        profile.context_path("log.md").write_text("Unrelated old notes.")
        seen = model(monkeypatch, {"recommendations": []})
        run_decision(book, profile=profile, today=TODAY)
        assert "Keep funds available for tuition." in seen[-1]
        assert "My horizon is five years." in seen[-1]
        assert "Unrelated old notes." not in seen[-1]
        profile.context_path("strategy.md").write_text(
            "Keep funds available for housing."
        )
        run_decision(book, profile=profile, today=TODAY)
        assert "Keep funds available for housing." in seen[-1]
        assert "Keep funds available for tuition." not in seen[-1]

    @pytest.mark.parametrize(
        "content", [None, "  ", "<!-- cash-ash:template -->\nExample restriction"]
    )
    def test_unwritten_context_is_explicitly_missing(
        self,
        book: sqlite3.Connection,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        content: str | None,
    ) -> None:
        profile = Profile(name="test", telegram_id=1, root=tmp_path)
        profile.context.mkdir()
        if content is not None:
            profile.context_path("investor.md").write_text(content)
        seen = model(monkeypatch, {"recommendations": []})
        run_decision(book, profile=profile, today=TODAY)
        assert '"investor.md": null' in seen[-1]
        assert '"strategy.md": null' in seen[-1]
        assert "Example restriction" not in seen[-1]

    def test_decision_sees_dated_native_quotes_fx_and_reserved_cash(
        self, book: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_prices(book, [(1, TODAY.isoformat(), 12.3456, "USD", "synthetic close")])
        save_fx_rate(
            book,
            rate_date=TODAY.isoformat(),
            base="USD",
            quote="EUR",
            rate=0.9,
            source="synthetic FX",
        )
        rec = recommendation(book)
        record_response(book, rec, "approve", today=TODAY)
        seen = model(monkeypatch, {"recommendations": []})
        run_decision(book, today=TODAY)
        text = seen[-1]
        assert '"price_native": 12.3456, "currency": "USD"' in text
        assert '"date": "2026-09-07", "source": "synthetic close"' in text
        assert '"fx_base": "USD", "fx_quote": "EUR"' in text
        assert (
            '"rate": 0.9, "rate_date": "2026-09-07", "source": "synthetic FX"' in text
        )
        assert "Reserved cash EUR 50.00 · available funded cash EUR 850.00" in text
        assert "Pending execution: TEST" in text

    def test_missing_quote_and_fx_are_not_rendered_as_zero(
        self, book: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        book.execute("DELETE FROM prices")
        book.execute("UPDATE securities SET currency='USD', pricing_mode='manual'")
        seen = model(monkeypatch, {"recommendations": []})
        run_decision(book, today=TODAY)
        assert '"latest_stored_close": null' in seen[-1]
        assert '"fx_to_eur": null' in seen[-1]
        assert '"pricing_mode": "manual"' in seen[-1]
        assert "TEST: unpriced" in seen[-1]

    def test_fresh_research_reaches_decision(
        self, book: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assessment(book)
        seen = model(
            monkeypatch,
            {
                "recommendations": [
                    dict(
                        ticker="TEST",
                        action="ADD",
                        amount_eur=50,
                        rationale="Revenue persists",
                    )
                ],
                "summary": "Review",
            },
        )
        _, recommendations, _ = run_decision(book, today=TODAY)
        assert not recommendations[0]["refused"]
        assert "Revenue observation" in seen[-1]
        assert "https://example.com/revenue" in seen[-1]
        assert "2026-09-07" in seen[-1]
        assert (
            '"fx_to_eur": {"rate": 1.0, "rate_date": null, "source": "EUR identity"}'
            in seen[-1]
        )

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"recommendations": "bad"},
            {
                "recommendations": [
                    dict(
                        ticker="TEST",
                        action="ADD",
                        amount_eur=float("nan"),
                        rationale="bad",
                    )
                ]
            },
            {
                "recommendations": [
                    dict(ticker=None, action="BUY", amount_eur=10, rationale="bad")
                ]
            },
        ],
    )
    def test_invalid_batch_preserves_prior_advice(
        self, book: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, payload: dict
    ) -> None:
        old = recommendation(book, action="REVIEW", amount=None)
        model(monkeypatch, payload)
        with pytest.raises(ValueError):
            run_decision(book, today=TODAY)
        assert (
            book.execute(
                "SELECT superseded_by_run_id FROM recommendation WHERE id=?", (old,)
            ).fetchone()[0]
            is None
        )

    def test_duplicates_rejected(
        self, book: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw = dict(ticker="TEST", action="ADD", amount_eur=50, rationale="Reason")
        model(monkeypatch, {"recommendations": [raw, raw]})
        with pytest.raises(ValueError, match="Duplicate"):
            run_decision(book, today=TODAY)

    def test_persistence_failure_rolls_back_retirement(
        self, book: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old = recommendation(book, action="REVIEW", amount=None)
        model(
            monkeypatch,
            {
                "recommendations": [
                    dict(
                        ticker="TEST",
                        action="REVIEW",
                        amount_eur=None,
                        rationale="Reason",
                    )
                ]
            },
        )

        def fail(*args: object, **kwargs: object) -> int:
            raise sqlite3.IntegrityError("synthetic failure")

        monkeypatch.setattr(decisions, "_store_recommendation", fail)
        with pytest.raises(sqlite3.IntegrityError):
            run_decision(book, today=TODAY)
        assert (
            book.execute(
                "SELECT superseded_by_run_id FROM recommendation WHERE id=?", (old,)
            ).fetchone()[0]
            is None
        )

    def test_missing_research_refusal_is_persisted(
        self, book: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        model(
            monkeypatch,
            {
                "recommendations": [
                    dict(ticker="TEST", action="ADD", amount_eur=50, rationale="Reason")
                ]
            },
        )
        _, rows, _ = run_decision(book, today=TODAY)
        assert rows[0]["refused"]
        assert book.execute("SELECT COUNT(*) FROM decision_refusal").fetchone()[0] == 1
        assert "Trade proposals blocked" in weekly_report(book, today=TODAY).body

    def test_weekly_execution_budget_survives_rerun(
        self, book: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assessment(book)
        rec = recommendation(book, amount=80)
        record_response(book, rec, "approve", today=TODAY)
        record_execution(book, rec, quantity=8, amount=80, day=TODAY.isoformat())
        model(
            monkeypatch,
            {
                "recommendations": [
                    dict(ticker="TEST", action="ADD", amount_eur=50, rationale="Reason")
                ]
            },
        )
        _, rows, _ = run_decision(book, today=TODAY)
        assert rows[0]["amount_eur"] == 20


class TestReviewHealth:
    def test_unreviewed_is_not_a_quiet_week(self, book: sqlite3.Connection) -> None:
        body = weekly_report(book, today=TODAY).body
        assert "No conclusion about this week" in body
        assert "Review completed; no action proposed" not in body

    def test_failure_persists_into_report(self, book: sqlite3.Connection) -> None:
        cycle = start_cycle(book, TODAY.isoformat())
        finish_cycle(
            book, cycle, [dict(name="research", ok=False, detail="provider failed")]
        )
        body = weekly_report(book, today=TODAY).body
        assert "incomplete" in body and "provider failed" in body

    def test_unpriced_holding_does_not_create_a_loss(
        self, book: sqlite3.Connection
    ) -> None:
        book.execute("DELETE FROM prices")
        body = weekly_report(book, today=TODAY).body
        assert "Aggregate return unavailable" in body
        assert "-100.0%" not in body
        assert "1 holding(s) could not be priced" in body
        assert build_guardrail_context(book, today=TODAY).blocked_reason

    def test_snooze_returns_when_due(self, book: sqlite3.Connection) -> None:
        rec = recommendation(book, action="REVIEW", amount=None)
        record_response(book, rec, "later", today=TODAY)
        assert not _pending_recommendations(book, today=TODAY)
        assert [
            r["id"]
            for r in _pending_recommendations(book, today=TODAY + timedelta(days=2))
        ] == [rec]

    def test_accepted_thesis_changes_only_on_owner_action(
        self, book: sqlite3.Connection
    ) -> None:
        current = active_thesis(book, security_id=1)
        run = create_research_run(book, run_date=TODAY.isoformat(), kind="deep")
        proposal = _propose_thesis(
            book,
            thesis=current,
            status="broken",
            summary=current.summary,
            reason="Revenue contracts",
            new_questions=(),
            run_id=run,
            llm_call_id=None,
        )
        assert active_thesis(book, security_id=1).id == current.id
        review_thesis(book, 1, proposal.version, True)
        assert active_thesis(book, security_id=1).id == proposal.id
        assert (
            book.execute(
                "SELECT status FROM thesis WHERE id=?", (current.id,)
            ).fetchone()[0]
            == "superseded"
        )

    def test_old_baseline_cannot_be_accepted(self, book: sqlite3.Connection) -> None:
        current = active_thesis(book, security_id=1)
        run = create_research_run(book, run_date=TODAY.isoformat(), kind="deep")
        proposal = _propose_thesis(
            book,
            thesis=current,
            status="broken",
            summary=current.summary,
            reason="Reason",
            new_questions=(),
            run_id=run,
            llm_call_id=None,
        )
        save_thesis(
            book, Thesis(security_id=1, summary="New owner reason", source="user")
        )
        with pytest.raises(ValueError, match="older thesis"):
            review_thesis(book, 1, proposal.version, True)


class TestEvidenceAndRotation:
    def test_invented_citation_is_unanswered(self) -> None:
        rows = validate_answers(
            [
                dict(
                    question="Q",
                    answer="Invented figure",
                    kind="sourced",
                    source_id="E99",
                    supporting_quote="quote",
                )
            ],
            [],
            ["Q"],
        )
        assert rows[0]["kind"] == "unanswered"
        assert rows[0]["source_url"] is None

    def test_source_date_and_url_come_from_package(self) -> None:
        item = EvidenceItem(
            title="Revenue persists",
            url="https://example.com/real",
            published="2026-09-01",
        )
        rows = validate_answers(
            [
                dict(
                    question="Q",
                    answer="Revenue persists",
                    kind="sourced",
                    source_id="E1",
                    supporting_quote="Revenue persists",
                    source_url="https://example.com/invented",
                    published_date="2099-01-01",
                )
            ],
            [item],
            ["Q"],
        )
        assert rows[0]["source_url"] == item.url
        assert rows[0]["published_date"] == item.published

    def test_omitted_question_stays_unanswered(self) -> None:
        rows = validate_answers(
            [dict(question="Q1", answer="General", kind="background")], [], ["Q1", "Q2"]
        )
        assert rows[-1]["question"] == "Q2" and rows[-1]["kind"] == "unanswered"

    def test_question_linked_excerpts_precede_news(self, tmp_path: Path) -> None:
        class Source:
            name = "test"

            def fetch(self, symbol: str, *, limit: int) -> list[EvidenceItem]:
                return [
                    EvidenceItem(
                        title="News",
                        url="https://example.com/news",
                        published="2026-09-05",
                    )
                ]

        path = tmp_path / "evidence.json"
        path.write_text(
            json.dumps(
                [
                    dict(
                        symbol="TEST",
                        questions=["Revenue"],
                        title="Release",
                        url="https://example.com/release",
                        published="2026-09-01",
                        excerpt="Revenue persists",
                    )
                ]
            )
        )
        items = gather_evidence(
            Source(), "TEST", ["Revenue?"], today=TODAY, evidence_file=path
        )
        assert items[0].title == "Release"
        items = gather_evidence(
            Source(), "TEST", ["Debt?"], today=TODAY, evidence_file=path
        )
        assert items[0].title == "News"

    def test_overdue_research_gets_a_slot_without_model_selection(
        self, book: sqlite3.Connection
    ) -> None:
        targets, _ = select_research(book, [], today=TODAY, limit=1)
        assert targets[0][0] == "TEST"
        assessment(book)
        assert select_research(book, [], today=TODAY, limit=1)[0] == []

    def test_recently_checked_holding_does_not_starve_another(
        self, book: sqlite3.Connection
    ) -> None:
        sid = upsert_security(
            book, Security(ticker="NEXT", name="Synthetic next", currency="EUR")
        )
        insert_trade(
            book,
            Trade(
                security_id=sid,
                trade_date=TODAY.isoformat(),
                side="BUY",
                quantity=1,
                amount_eur=10,
            ),
        )
        save_thesis(book, Thesis(security_id=sid, summary="Reason", source="user"))
        assessment(book, ticker="TEST")
        targets, _ = select_research(book, [("TEST", "Event")], today=TODAY, limit=2)
        assert {t for t, _ in targets} == {"TEST", "NEXT"}

    def test_unsupported_asset_is_deferred(self, book: sqlite3.Connection) -> None:
        book.execute("UPDATE securities SET asset_class='fund'")
        targets, deferred = select_research(
            book, [("TEST", "Event")], today=TODAY, limit=1
        )
        assert targets == [] and deferred == ["TEST"]


class TestUpgradeAndNotifications:
    def test_upgrade_preserves_existing_evidence_and_decisions(self) -> None:
        from db.migrations import discover_migrations

        # Deliberately skip auto-migration to construct the previous schema.
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        migrations = discover_migrations()
        target = next(
            i for i, m in enumerate(migrations) if m.key.endswith("011_review_workflow")
        )
        for migration in migrations[:target]:
            migration.upgrade(conn)
        conn.execute(
            "INSERT INTO evidence(claim,kind,created_at) VALUES ('Synthetic background','background','2026-09-01')"
        )
        conn.execute(
            "INSERT INTO recommendation(run_date,action,rationale,expires_on,created_at) VALUES ('2026-09-01','REVIEW','Synthetic reason','2026-09-08','2026-09-01')"
        )
        conn.execute(
            "INSERT INTO user_decision(recommendation_id,decision,decided_at) VALUES (1,'later','2026-09-01')"
        )
        migrations[target].upgrade(conn)
        assert (
            conn.execute("SELECT claim FROM evidence").fetchone()[0]
            == "Synthetic background"
        )
        assert (
            conn.execute("SELECT decision FROM user_decision").fetchone()[0] == "later"
        )
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        # Historical cycles must not be guessed as successful.
        assert conn.execute("SELECT COUNT(*) FROM review_cycle").fetchone()[0] == 0
        conn.close()

    def test_snooze_reminder_sent_once(
        self, book: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        import config
        import reminders

        rec = recommendation(book, action="REVIEW", amount=None)
        record_response(book, rec, "later", today=TODAY)
        book.execute("UPDATE user_decision SET snoozed_until='2026-09-07'")
        profile = SimpleNamespace(enabled=True, telegram_id=123, db=Path("/unused"))
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "synthetic")
        monkeypatch.setattr(reminders, "load_profiles", lambda: {"synthetic": profile})
        monkeypatch.setattr(reminders, "open_existing_db", lambda path: book)

        class FixedDate(date):
            @classmethod
            def today(cls) -> date:
                return TODAY

        monkeypatch.setattr(reminders, "date", FixedDate)
        sent: list[dict] = []
        monkeypatch.setattr(
            reminders, "send_with_buttons", lambda **kwargs: sent.append(kwargs)
        )
        reminders.send_due_snoozes()
        reminders.send_due_snoozes()
        assert len(sent) == 1
        assert "Snoozed review is due" in sent[0]["text"]
        assert "View evidence" in str(sent[0]["buttons"])

    def test_action_buttons_distinguish_review_from_trade(self) -> None:
        from review_text import recommendation_buttons

        review = recommendation_buttons(dict(id=1, action="REVIEW", ticker="TEST"))
        trade = recommendation_buttons(dict(id=2, action="ADD", ticker="TEST"))
        assert review[0][0][0] == "Acknowledge"
        assert trade[0][0][0] == "Approve trade"

    def test_failed_delivery_does_not_consume_reminder(
        self, book: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        import config
        import reminders

        rec = recommendation(book, action="REVIEW", amount=None, expires="2099-01-01")
        record_response(book, rec, "later", today=TODAY)
        with book:
            book.execute("UPDATE user_decision SET snoozed_until='2020-01-01'")
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "synthetic")
        monkeypatch.setattr(
            reminders,
            "load_profiles",
            lambda: {
                "synthetic": SimpleNamespace(
                    enabled=True, telegram_id=123, db=Path("/unused")
                )
            },
        )
        monkeypatch.setattr(reminders, "open_existing_db", lambda path: book)

        def fail(**kwargs: object) -> None:
            raise RuntimeError("synthetic network failure")

        monkeypatch.setattr(reminders, "send_with_buttons", fail)
        with pytest.raises(RuntimeError):
            reminders.send_due_snoozes()
        assert (
            book.execute("SELECT reminder_sent_at FROM user_decision").fetchone()[0]
            is None
        )

    def test_buy_without_quote_is_refused(
        self, book: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sid = upsert_security(
            book, Security(ticker="NEW", name="Synthetic new", currency="EUR")
        )
        save_thesis(book, Thesis(security_id=sid, summary="Reason", source="user"))
        assessment(book, ticker="NEW")
        model(
            monkeypatch,
            {
                "recommendations": [
                    dict(ticker="NEW", action="BUY", amount_eur=50, rationale="Reason")
                ]
            },
        )
        _, rows, _ = run_decision(book, today=TODAY)
        assert rows[0]["refused"] and "No quote" in rows[0]["refusal"]


class TestProcessRegressions:
    def test_json_braces_inside_rationale_are_valid(self) -> None:
        from research import extract_json

        assert (
            extract_json('{"rationale":"a } brace and { another"}')["rationale"]
            == "a } brace and { another"
        )

    def test_string_false_is_not_a_selection(
        self, book: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import research

        monkeypatch.setattr(
            research,
            "call_llm",
            lambda *args, **kwargs: LLMResult(
                text=json.dumps(
                    {
                        "rankings": [
                            dict(
                                ticker="TEST",
                                selected="false",
                                rank=1,
                                reason="Nothing",
                            )
                        ]
                    }
                ),
                model="test",
                requested_model="test",
            ),
        )
        with pytest.raises(ValueError, match="boolean"):
            research.run_triage(book, today=TODAY)

    def test_unused_capacity_checks_more_overdue_holdings(
        self, book: sqlite3.Connection
    ) -> None:
        sid = upsert_security(
            book, Security(ticker="NEXT", name="Synthetic next", currency="EUR")
        )
        insert_trade(
            book,
            Trade(
                security_id=sid,
                trade_date=TODAY.isoformat(),
                side="BUY",
                quantity=1,
                amount_eur=10,
            ),
        )
        save_thesis(book, Thesis(security_id=sid, summary="Reason", source="user"))
        targets, _ = select_research(book, [], today=TODAY, limit=4)
        assert {t for t, _ in targets} == {"TEST", "NEXT"}
