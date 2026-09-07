"""Tests for the deterministic constraints on what may be recommended.

These are the rules that make an LLM safe to have near a portfolio decision.
A prompt asking a model to respect a position limit is a request; these are not.
"""

from __future__ import annotations

import pytest

from config import MAX_NEW_TRADE_EUR, MAX_POSITION_WEIGHT_PCT, MAX_WEEKLY_ALLOCATION_EUR
from guardrails import GuardrailContext, check_proposal, is_large_position


def _context(**kw) -> GuardrailContext:
    base = {
        "total_value_eur": 1000.0,
        "cash_eur": 10.0,
        "monthly_contribution_eur": 150.0,
        "weights_by_ticker": {"BIG": 19.5, "MID": 10.0, "SMALL": 3.0},
        "values_by_ticker": {"BIG": 195.0, "MID": 100.0, "SMALL": 30.0},
        "thesis_status_by_ticker": {
            "BIG": "unexamined",
            "MID": "unchanged",
            "SMALL": "broken",
        },
    }
    base.update(kw)
    return GuardrailContext(**base)


class TestSellDiscipline:
    """The owner's written rule: price alone is never a reason to sell."""

    def test_exit_on_an_unexamined_thesis_is_refused(self) -> None:
        verdict = check_proposal(
            action="EXIT", ticker="MID", amount_eur=None, context=_context()
        )
        assert verdict.refused
        assert "price alone is never a reason" in verdict.refusal

    def test_trim_on_an_intact_thesis_is_refused(self) -> None:
        verdict = check_proposal(
            action="TRIM", ticker="MID", amount_eur=None, context=_context()
        )
        assert verdict.refused

    def test_exit_on_a_broken_thesis_is_allowed(self) -> None:
        verdict = check_proposal(
            action="EXIT", ticker="SMALL", amount_eur=None, context=_context()
        )
        assert not verdict.refused
        assert verdict.action == "EXIT"

    def test_trim_on_a_deteriorating_thesis_is_allowed(self) -> None:
        context = _context(thesis_status_by_ticker={"MID": "deteriorating"})
        verdict = check_proposal(
            action="TRIM", ticker="MID", amount_eur=None, context=context
        )
        assert not verdict.refused

    def test_oversized_position_may_be_trimmed_without_a_thesis_change(self) -> None:
        # Trimming for size is a portfolio decision, not a view on the company.
        context = _context(
            weights_by_ticker={"BIG": 25.0},
            thesis_status_by_ticker={"BIG": "unchanged"},
        )
        verdict = check_proposal(
            action="TRIM", ticker="BIG", amount_eur=None, context=context
        )
        assert not verdict.refused

    def test_exit_on_an_oversized_intact_position_becomes_a_trim(self) -> None:
        # Being too large justifies trimming, never closing.
        context = _context(
            weights_by_ticker={"BIG": 25.0},
            thesis_status_by_ticker={"BIG": "unchanged"},
        )
        verdict = check_proposal(
            action="EXIT", ticker="BIG", amount_eur=None, context=context
        )
        assert verdict.action == "TRIM"
        assert verdict.adjusted

    def test_a_large_loss_does_not_justify_selling(self) -> None:
        # The whole point: a 38% fall with an intact thesis is a price change.
        context = _context(
            thesis_status_by_ticker={"LOSER": "unexamined"},
            weights_by_ticker={"LOSER": 3.9},
        )
        verdict = check_proposal(
            action="EXIT", ticker="LOSER", amount_eur=None, context=context
        )
        assert verdict.refused


class TestBuyLimits:
    """Position caps, trade size, and capital that does not exist."""

    def test_add_to_a_capped_position_is_refused(self) -> None:
        context = _context(weights_by_ticker={"BIG": MAX_POSITION_WEIGHT_PCT})
        verdict = check_proposal(
            action="ADD", ticker="BIG", amount_eur=50.0, context=context
        )
        assert verdict.refused
        assert "cap" in verdict.refusal

    def test_add_is_clamped_to_the_weight_headroom(self) -> None:
        verdict = check_proposal(
            action="ADD", ticker="BIG", amount_eur=100.0, context=_context()
        )
        assert verdict.amount_eur < 100.0
        assert verdict.adjusted

    def test_cash_purchase_preserves_total_wealth(self) -> None:
        context = _context(
            total_value_eur=1000,
            cash_eur=100,
            weights_by_ticker={"BIG": 19},
            values_by_ticker={"BIG": 190},
        )
        verdict = check_proposal(
            action="ADD", ticker="BIG", amount_eur=100, context=context
        )
        assert verdict.amount_eur == 10
        assert (190 + verdict.amount_eur) / 1000 * 100 == MAX_POSITION_WEIGHT_PCT

    def test_single_trade_limit_binds(self) -> None:
        verdict = check_proposal(
            action="BUY",
            ticker="NEW",
            amount_eur=10_000.0,
            context=_context(cash_eur=500),
        )
        assert verdict.amount_eur == min(MAX_NEW_TRADE_EUR, MAX_WEEKLY_ALLOCATION_EUR)

    def test_available_capital_binds(self) -> None:
        context = _context(cash_eur=20.0, monthly_contribution_eur=150.0)
        verdict = check_proposal(
            action="BUY", ticker="NEW", amount_eur=100.0, context=context
        )
        assert verdict.amount_eur == 20.0

    def test_weekly_allocation_is_consumed_across_proposals(self) -> None:
        context = _context(allocated_this_run_eur=MAX_WEEKLY_ALLOCATION_EUR)
        verdict = check_proposal(
            action="BUY", ticker="NEW", amount_eur=50.0, context=context
        )
        assert verdict.refused
        assert "no capital left" in verdict.refusal

    def test_missing_amount_is_refused(self) -> None:
        verdict = check_proposal(
            action="ADD", ticker="MID", amount_eur=None, context=_context()
        )
        assert verdict.refused

    def test_zero_amount_is_refused(self) -> None:
        verdict = check_proposal(
            action="BUY", ticker="NEW", amount_eur=0.0, context=_context()
        )
        assert verdict.refused


class TestAvailableCapital:
    """BUY and ADD are unreachable without the expected contribution."""

    def test_excludes_the_planned_contribution(self) -> None:
        assert (
            _context(
                cash_eur=10.0, monthly_contribution_eur=150.0
            ).available_capital_eur
            == 10.0
        )

    def test_without_a_contribution_only_cash_is_available(self) -> None:
        context = _context(cash_eur=4.15, monthly_contribution_eur=0.0)
        assert context.available_capital_eur == 4.15


class TestPassThrough:
    """Actions with no size or portfolio effect are not interfered with."""

    @pytest.mark.parametrize("action", ["HOLD", "REVIEW", "KEEP_CASH"])
    def test_untouched(self, action: str) -> None:
        verdict = check_proposal(
            action=action, ticker="MID", amount_eur=None, context=_context()
        )
        assert not verdict.refused
        assert verdict.action == action


class TestLargePosition:
    """A flag, not a limit: adding here needs a better argument."""

    def test_large(self) -> None:
        assert is_large_position("BIG", _context()) is True

    def test_small(self) -> None:
        assert is_large_position("SMALL", _context()) is False

    def test_unknown_ticker_is_not_large(self) -> None:
        assert is_large_position("NOPE", _context()) is False
