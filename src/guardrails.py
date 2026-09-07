"""Deterministic constraints on what may be recommended.

These run in code, after the model has proposed, and they are the reason an
LLM is allowed near a portfolio decision at all. A prompt asking a model to
respect a position limit is a request; this is not.

Two kinds of rule live here. Portfolio limits — position weight, trade size,
capital available — are arithmetic, and a proposal that breaches one is clamped
or refused. The owner's own sell discipline is the second kind, and it is the
more important: their written strategy says price alone is never a reason to
sell, so an EXIT or TRIM must rest on a thesis that has actually deteriorated
or on a position that has actually grown too large. A recommendation to sell
because a number fell is refused here, not argued with.

Public API:
    GuardrailContext  -- portfolio state the checks run against
    Verdict           -- what a check concluded about one proposal
    check_proposal    -- apply every rule to one proposed recommendation
    available_capital -- funded cash less outstanding approvals

Example:
    from guardrails import check_proposal

    verdict = check_proposal(proposal, context=context)
    if verdict.refused:
        ...
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from config import (
    LARGE_POSITION_WEIGHT_PCT,
    MAX_NEW_TRADE_EUR,
    MAX_POSITION_WEIGHT_PCT,
    MAX_WEEKLY_ALLOCATION_EUR,
)

logger = logging.getLogger(__name__)

BUY_ACTIONS = frozenset({"BUY", "ADD"})
SELL_ACTIONS = frozenset({"TRIM", "EXIT"})

_THESIS_SUPPORTS_SELLING = frozenset({"deteriorating", "broken"})
"""Thesis states that can justify selling.

From the owner's written strategy: they sell when the reason they bought stops
being true, and never because a price moved. A thesis that is unexamined,
unchanged or improving cannot support an EXIT, however bad the chart looks.
"""


@dataclass(frozen=True)
class GuardrailContext:
    """Portfolio state the checks are applied against."""

    total_value_eur: float
    cash_eur: float
    monthly_contribution_eur: float
    weights_by_ticker: dict[str, float] = field(default_factory=dict)
    values_by_ticker: dict[str, float] = field(default_factory=dict)
    thesis_status_by_ticker: dict[str, str] = field(default_factory=dict)
    allocated_this_run_eur: float = 0.0
    reserved_cash_eur: float = 0.0
    weekly_committed_eur: float = 0.0
    blocked_reason: str | None = None
    pending_tickers: frozenset[str] = frozenset()

    @property
    def available_capital_eur(self) -> float:
        """Cash actually funded, less approved trades still awaiting execution."""
        return max(0.0, self.cash_eur - self.reserved_cash_eur)


@dataclass(frozen=True)
class Verdict:
    """What the checks concluded about one proposal."""

    action: str
    amount_eur: float | None
    refused: bool = False
    refusal: str | None = None
    adjustments: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()

    @property
    def adjusted(self) -> bool:
        """True when the proposal was allowed but not as asked."""
        return bool(self.adjustments)


def check_proposal(
    *,
    action: str,
    ticker: str | None,
    amount_eur: float | None,
    context: GuardrailContext,
) -> Verdict:
    """Apply every deterministic rule to one proposed recommendation.

    Args:
        action: Proposed action.
        ticker: Holding it concerns, or None for a portfolio-level action.
        amount_eur: Proposed size, where the action has one.
        context: Portfolio state to check against.

    Returns:
        The verdict, with the amount clamped where a limit allowed a smaller
        version of the same action.
    """
    checks: list[str] = []
    adjustments: list[str] = []
    amount = amount_eur
    if action in BUY_ACTIONS | SELL_ACTIONS:
        if ticker in context.pending_tickers:
            return Verdict(
                action,
                None,
                refused=True,
                refusal="Approved action still awaiting execution; resolve it first.",
            )
        if context.blocked_reason:
            return Verdict(action, None, refused=True, refusal=context.blocked_reason)
        if not ticker:
            return Verdict(
                action, None, refused=True, refusal="Trade requires a ticker."
            )
        if amount is not None and (not math.isfinite(amount) or amount <= 0):
            return Verdict(
                action,
                None,
                refused=True,
                refusal="Trade amount must be finite and positive.",
            )
        if action == "ADD" and ticker not in context.values_by_ticker:
            return Verdict(
                action, None, refused=True, refusal="ADD requires a priced holding."
            )
        if action == "BUY" and ticker in context.values_by_ticker:
            return Verdict(
                action, None, refused=True, refusal="Use ADD for an existing holding."
            )
        if action in SELL_ACTIONS:
            held = context.values_by_ticker.get(ticker)
            if held is None or held <= 0:
                return Verdict(
                    action,
                    None,
                    refused=True,
                    refusal="Sale requires a priced holding.",
                )
            if amount is None:
                amount = held if action == "EXIT" else min(held, MAX_NEW_TRADE_EUR)
            if action == "TRIM" and amount > min(held, MAX_NEW_TRADE_EUR):
                amount = min(held, MAX_NEW_TRADE_EUR)
                adjustments.append(
                    "Sale reduced to holding value and single-trade limit."
                )
            if action == "EXIT":
                amount = held

    if action in SELL_ACTIONS and ticker:
        status = context.thesis_status_by_ticker.get(ticker, "unexamined")
        weight = context.weights_by_ticker.get(ticker, 0.0)
        oversized = weight > MAX_POSITION_WEIGHT_PCT
        checks.append(f"thesis status is {status}")
        checks.append(
            f"weight {weight:.1f}% against {MAX_POSITION_WEIGHT_PCT:.0f}% cap"
        )

        if status not in _THESIS_SUPPORTS_SELLING and not oversized:
            return Verdict(
                action=action,
                amount_eur=None,
                refused=True,
                refusal=(
                    f"{action} refused: the thesis is '{status}', not deteriorating "
                    f"or broken, and the position is within its weight cap. The "
                    f"strategy says price alone is never a reason to sell."
                ),
                checks=tuple(checks),
            )
        if action == "EXIT" and status == "deteriorating":
            action = "TRIM"
            amount = min(amount, MAX_NEW_TRADE_EUR)
            adjustments.append(
                "EXIT reduced to TRIM: thesis deteriorating, not broken."
            )
        if status not in _THESIS_SUPPORTS_SELLING and oversized and action == "EXIT":
            adjustments.append(
                "EXIT reduced to TRIM: the position is oversized, which justifies "
                "trimming for size, but the thesis has not broken."
            )
            action = "TRIM"
            amount = min(
                amount,
                MAX_NEW_TRADE_EUR,
                max(
                    0,
                    context.values_by_ticker[ticker]
                    - context.total_value_eur * MAX_POSITION_WEIGHT_PCT / 100,
                ),
            )
        if oversized and status not in _THESIS_SUPPORTS_SELLING:
            target = (
                context.values_by_ticker[ticker]
                - context.total_value_eur * MAX_POSITION_WEIGHT_PCT / 100
            )
            if amount > target:
                amount = target
                adjustments.append("Trim limited to the amount above the weight cap.")

    if action in BUY_ACTIONS:
        available = context.available_capital_eur - context.allocated_this_run_eur
        weekly_left = (
            MAX_WEEKLY_ALLOCATION_EUR
            - context.weekly_committed_eur
            - context.allocated_this_run_eur
        )
        checks.append(f"EUR {available:.2f} capital left this run")
        checks.append(f"EUR {weekly_left:.2f} of the weekly allocation left")

        if amount is None or amount <= 0:
            return Verdict(
                action=action,
                amount_eur=None,
                refused=True,
                refusal=f"{action} refused: no amount proposed.",
                checks=tuple(checks),
            )

        if ticker:
            weight = context.weights_by_ticker.get(ticker, 0.0)
            checks.append(
                f"weight {weight:.1f}% against {MAX_POSITION_WEIGHT_PCT:.0f}% cap"
            )
            if weight >= MAX_POSITION_WEIGHT_PCT:
                return Verdict(
                    action=action,
                    amount_eur=None,
                    refused=True,
                    refusal=(
                        f"{action} refused: {ticker} is already {weight:.1f}% of the "
                        f"portfolio, at or over the {MAX_POSITION_WEIGHT_PCT:.0f}% cap."
                    ),
                    checks=tuple(checks),
                )
            headroom = _headroom_to_cap(ticker, context)
            if headroom is not None and amount > headroom:
                adjustments.append(
                    f"reduced to EUR {headroom:.2f}, the most that keeps {ticker} "
                    f"under the {MAX_POSITION_WEIGHT_PCT:.0f}% cap"
                )
                amount = headroom

        for limit, label in (
            (MAX_NEW_TRADE_EUR, "single-trade limit"),
            (weekly_left, "weekly allocation"),
            (available, "available capital"),
        ):
            if amount > limit:
                adjustments.append(f"reduced to EUR {max(limit, 0):.2f} by the {label}")
                amount = max(limit, 0.0)

        if amount <= 0:
            return Verdict(
                action=action,
                amount_eur=None,
                refused=True,
                refusal=(
                    f"{action} refused: no capital left this run once the weekly "
                    f"allocation and available cash are applied."
                ),
                checks=tuple(checks),
            )

    return Verdict(
        action=action,
        amount_eur=amount,
        adjustments=tuple(adjustments),
        checks=tuple(checks),
    )


def _headroom_to_cap(ticker: str, context: GuardrailContext) -> float | None:
    """Return headroom when buying transfers existing cash into a holding."""
    total = context.total_value_eur
    if total <= 0:
        return 0.0
    return max(
        0.0,
        MAX_POSITION_WEIGHT_PCT / 100 * total
        - context.values_by_ticker.get(ticker, 0.0),
    )


def is_large_position(ticker: str, context: GuardrailContext) -> bool:
    """True when a holding is large enough that adding needs a better argument."""
    return context.weights_by_ticker.get(ticker, 0.0) >= LARGE_POSITION_WEIGHT_PCT
