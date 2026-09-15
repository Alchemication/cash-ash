"""Tests for the agreed definitions of a good buy and a good sell."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from buy_sell_rules import sell_path
from cmd_thesis import record_examined
from config import CONCENTRATION_ALERT_PCT, MAX_NEW_TRADE_EUR, MIN_TRADE_EUR
from decisions import build_guardrail_context
from evals import observe
from guardrails import GuardrailContext, Verdict, check_proposal
from models import Thesis
from seed import load_snapshot, seed_database
from store_research import active_thesis, save_thesis
from tests.test_review_workflow import TODAY, book, recommendation  # noqa: F401
from tests.test_seed import FIXTURE
from workflow import record_response


def _context(**kw: object) -> GuardrailContext:
    base: dict[str, object] = dict(
        total_value_eur=1000.0,
        cash_eur=500.0,
        monthly_contribution_eur=150.0,
        weights_by_ticker={"HELD": 10.0},
        values_by_ticker={"HELD": 100.0},
        thesis_status_by_ticker={"HELD": "unchanged"},
        conviction_by_ticker={"HELD": "moderate"},
        themes_by_ticker={"HELD": ("tech",)},
        theme_values_eur={"tech": 100.0},
        valuation_checked=frozenset({"HELD"}),
    )
    base.update(kw)
    return GuardrailContext(**base)  # type: ignore[arg-type]


def _add(context: GuardrailContext, amount: float = 80.0) -> Verdict:
    return check_proposal(
        action="ADD", ticker="HELD", amount_eur=amount, context=context
    )


def _sell(context: GuardrailContext, action: str = "EXIT") -> Verdict:
    return check_proposal(
        action=action, ticker="HELD", amount_eur=None, context=context
    )


class TestGoodBuy:
    def test_every_check_passing_allows_the_add(self) -> None:
        verdict = _add(_context())
        assert not verdict.refused
        assert "reason: rated moderate" in verdict.checks

    def test_a_weak_reason_is_refused_by_name(self) -> None:
        verdict = _add(_context(conviction_by_ticker={"HELD": "weak"}))
        assert verdict.refused
        assert "reason: rated 'weak', needs moderate or better" in verdict.refusal

    def test_an_unchecked_valuation_refuses_rather_than_passes(self) -> None:
        # Until filing data exists no buy can have its price checked, and
        # refusing by name beats recommending one nobody examined.
        verdict = _add(_context(valuation_checked=frozenset()))
        assert verdict.refused
        assert "price: valuation not checked" in verdict.refusal

    def test_every_failing_check_is_named_at_once(self) -> None:
        verdict = _add(
            _context(
                conviction_by_ticker={"HELD": "none"}, valuation_checked=frozenset()
            )
        )
        assert "reason:" in verdict.refusal
        assert "price:" in verdict.refusal

    def test_a_theme_at_the_alert_level_refuses(self) -> None:
        verdict = _add(
            _context(theme_values_eur={"tech": CONCENTRATION_ALERT_PCT * 10})
        )
        assert verdict.refused
        assert "fit: theme tech" in verdict.refusal

    def test_an_amount_is_cut_to_keep_its_theme_under_the_alert(self) -> None:
        # 40% of 1000 is 400; 330 already in the theme leaves 70.
        verdict = _add(_context(theme_values_eur={"tech": 330.0}), amount=100.0)
        assert not verdict.refused
        assert verdict.amount_eur == pytest.approx(70.0)

    def test_a_trade_cut_below_the_minimum_is_refused(self) -> None:
        # A token trade pays conversion and commission on almost nothing.
        verdict = _add(_context(theme_values_eur={"tech": 370.0}), amount=100.0)
        assert verdict.refused
        assert f"below the EUR {MIN_TRADE_EUR:.0f} minimum trade" in verdict.refusal


class TestGoodSell:
    def test_price_alone_still_sells_nothing(self) -> None:
        verdict = _sell(_context())
        assert verdict.refused
        assert "no sell path applies" in verdict.refusal

    def test_an_examined_holding_with_no_reason_may_be_sold(self) -> None:
        context = _context(conviction_by_ticker={"HELD": "none"})
        verdict = _sell(context)
        assert not verdict.refused
        assert verdict.action == "EXIT"
        assert sell_path("HELD", context).name == "examined, no reason"

    def test_no_reason_nobody_examined_does_not_sell(self) -> None:
        # A bootstrap restating old notes is not an examination.
        context = _context(
            conviction_by_ticker={"HELD": "none"},
            thesis_status_by_ticker={"HELD": "unexamined"},
        )
        assert _sell(context).refused

    def test_an_excess_new_money_can_dilute_is_left_alone(self) -> None:
        # 250 against a 200 cap is 50 over: well inside three months of money.
        context = _context(
            weights_by_ticker={"HELD": 25.0}, values_by_ticker={"HELD": 250.0}
        )
        assert _sell(context, "TRIM").refused

    def test_a_position_far_over_the_cap_trims_only(self) -> None:
        # 20% of 4000 is 800; 1400 held is 600 over, more than 450 of new money.
        context = _context(
            total_value_eur=4000.0,
            weights_by_ticker={"HELD": 35.0},
            values_by_ticker={"HELD": 1400.0},
        )
        verdict = _sell(context)
        assert not verdict.refused
        assert verdict.action == "TRIM"
        assert verdict.amount_eur <= MAX_NEW_TRADE_EUR


class TestAgreedDefaults:
    """The real defaults, without the workflow tests' stand-ins."""

    def test_nothing_supplies_valuation_evidence_yet(
        self,
        book: sqlite3.Connection,  # noqa: F811
    ) -> None:
        context = build_guardrail_context(book, today=TODAY)
        verdict = check_proposal(
            action="ADD", ticker="TEST", amount_eur=60.0, context=context
        )
        assert verdict.refused
        assert "price: valuation not checked" in verdict.refusal

    def test_a_buy_cannot_be_approved_the_day_it_is_proposed(
        self,
        book: sqlite3.Connection,  # noqa: F811
    ) -> None:
        rec = recommendation(book)
        with pytest.raises(ValueError, match="Cooling off"):
            record_response(book, rec, "approve", today=TODAY)


class TestExamine:
    @pytest.fixture
    def seeded(self, conn: sqlite3.Connection) -> sqlite3.Connection:
        seed_database(conn, load_snapshot(FIXTURE))
        save_thesis(
            conn,
            Thesis(
                security_id=1,
                summary="Someone else bought it",
                source="user",
                conviction="weak",
                what_would_break_it=("they stop making it",),
            ),
        )
        return conn

    def test_examining_records_the_owners_words_and_keeps_conditions(
        self, seeded: sqlite3.Connection
    ) -> None:
        examined = record_examined(
            seeded,
            security_id=1,
            conviction="none",
            summary="No reason beyond wanting to own it.",
        )
        current = active_thesis(seeded, security_id=1)
        assert current.id == examined.id
        assert current.conviction == "none"
        assert current.thesis_status != "unexamined"
        assert current.source == "user"
        assert current.what_would_break_it == ("they stop making it",)

    def test_a_blank_reason_is_refused(self, seeded: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="own words"):
            record_examined(seeded, security_id=1, conviction="weak", summary="  ")


class TestNeverAReason:
    def test_a_price_move_given_as_the_reason_is_listed(
        self, conn: sqlite3.Connection
    ) -> None:
        conn.execute(
            """INSERT INTO recommendation (run_date, action, amount_eur, rationale,
            urgency, expires_on, created_at) VALUES ('2026-09-07', 'ADD', 50,
            'The shares have fallen 20%, a chance to average down.', 'low',
            '2026-09-14', 'x')"""
        )
        found = {item.name: item for item in observe(conn, today=date(2026, 9, 7))}
        listed = found["trade reasons that may be on the never-a-reason list"]
        assert "recommendation 1" in str(listed)
