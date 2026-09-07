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
    available_capital -- cash plus the planning contribution

Example:
    from guardrails import check_proposal

    verdict = check_proposal(proposal, context=context)
    if verdict.refused:
        ...
"""

from __future__ import annotations

import logging
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

    @property
    def available_capital_eur(self) -> float:
        """Cash plus the planning contribution.

        The contribution is included because without it BUY and ADD are
        unreachable — this portfolio holds almost no cash — and a decision
        layer that can only ever say HOLD or SELL is half a system.
        """
        return self.cash_eur + self.monthly_contribution_eur


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
        if status not in _THESIS_SUPPORTS_SELLING and oversized and action == "EXIT":
            adjustments.append(
                "EXIT reduced to TRIM: the position is oversized, which justifies "
                "trimming for size, but the thesis has not broken."
            )
            action = "TRIM"

    if action in BUY_ACTIONS:
        available = context.available_capital_eur - context.allocated_this_run_eur
        weekly_left = MAX_WEEKLY_ALLOCATION_EUR - context.allocated_this_run_eur
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
    """Return the largest addition that keeps *ticker* under the weight cap.

    Solved rather than approximated: adding to a position raises both the
    holding and the portfolio total, so the naive "cap percentage of today's
    total, minus what is held" overstates the room available.

    Args:
        ticker: Holding being added to.
        context: Portfolio state.

    Returns:
        The headroom in EUR, or None when it cannot be computed.
    """
    held = context.values_by_ticker.get(ticker)
    total = context.total_value_eur
    if held is None or total <= 0:
        return None
    cap = MAX_POSITION_WEIGHT_PCT / 100
    if cap >= 1:
        return None
    # (held + x) / (total + x) = cap  ->  x = (cap*total - held) / (1 - cap)
    headroom = (cap * total - held) / (1 - cap)
    return max(headroom, 0.0)


def is_large_position(ticker: str, context: GuardrailContext) -> bool:
    """True when a holding is large enough that adding needs a better argument."""
    return context.weights_by_ticker.get(ticker, 0.0) >= LARGE_POSITION_WEIGHT_PCT
