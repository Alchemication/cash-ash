"""What counts as a good buy and a good sell, as checks code can run.

These are the owner's agreed defaults, built from their written strategy and
mainstream long-horizon practice. They define a disciplined decision, not a
profitable one: no checklist makes a learning portfolio's stock picks beat an
index, and the benchmark exists to say whether they do. The model proposes and
explains; these decide whether a trade may be recommended at all.

A buy needs every check to pass:

- **reason** — a thesis rated moderate or better
- **price** — valuation checked against the company's own history. Nothing
  supplies that evidence yet, so the check fails by name until filing retrieval
  exists, rather than letting an unexamined buy through
- **fit** — the position under the position cap, and every theme it carries
  under the concentration alert level; an amount is cut to fit
- **size** — at least ``MIN_TRADE_EUR`` once every limit has been applied
- **cooling off** — approved no sooner than ``BUY_COOLING_OFF_DAYS`` after it
  was proposed, enforced at approval in ``workflow``

A sale needs one of three paths:

- **reason broke** — a broken thesis may exit; a deteriorating one may trim
- **examined, no reason** — the owner examined the holding and recorded that
  there is no reason to own it: conviction ``none`` on a thesis that is no
  longer ``unexamined``. A bootstrap restating old notes is not an examination
- **too big** — over the position cap by more than ``TRIM_CONTRIBUTION_MONTHS``
  of new money, which could otherwise dilute it; trims the excess only

Public API:
    DEFENSIBLE_CONVICTION -- conviction levels that count as a reason
    BuyChecks             -- which amount-independent buy checks passed
    SellPath              -- the path that permits selling one holding
    buy_checks            -- run the amount-independent buy checks
    theme_headroom        -- most EUR that keeps every theme under the alert
    sell_path             -- which sell path applies to a holding, if any
    valuation_evidence    -- tickers whose valuation has been checked

Example:
    from buy_sell_rules import buy_checks

    checks = buy_checks("TEST", context)
    if checks.failed:
        ...
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from config import (
    CONCENTRATION_ALERT_PCT,
    MAX_POSITION_WEIGHT_PCT,
    TRIM_CONTRIBUTION_MONTHS,
)

if TYPE_CHECKING:
    from guardrails import GuardrailContext

DEFENSIBLE_CONVICTION = frozenset({"moderate", "strong"})
"""Conviction levels that count as a reason the owner could defend.

``weak`` is a reason not tied to the business or its price and ``none`` is no
reason at all; neither supports buying more. The levels are defined in the
thesis bootstrap prompt.
"""


@dataclass(frozen=True)
class BuyChecks:
    """The buy checks that do not depend on the amount.

    Attributes:
        failed: One line per failing check, each starting with its name.
        passed: One line per passing check, recorded with the recommendation.
    """

    failed: tuple[str, ...]
    passed: tuple[str, ...]


@dataclass(frozen=True)
class SellPath:
    """The path that permits selling one holding.

    Attributes:
        name: How the path is named in checks and refusals.
        exit_allowed: False when the path justifies trimming but not closing.
        max_eur: Most the path allows selling, or None for no path-specific
            limit.
    """

    name: str
    exit_allowed: bool
    max_eur: float | None = None


def buy_checks(ticker: str, context: GuardrailContext) -> BuyChecks:
    """Run the reason, price and fit checks for buying *ticker*.

    Every failing check is reported, not only the first, so a refusal says
    everything that stands between the holding and a buy.

    Args:
        ticker: Holding or security to buy.
        context: Portfolio state to check against.

    Returns:
        Which checks failed and which passed.
    """
    failed: list[str] = []
    passed: list[str] = []

    conviction = context.conviction_by_ticker.get(ticker)
    if conviction is None:
        failed.append("reason: no thesis for this holding")
    elif conviction in DEFENSIBLE_CONVICTION:
        passed.append(f"reason: rated {conviction}")
    else:
        failed.append(f"reason: rated '{conviction}', needs moderate or better")

    if ticker in context.valuation_checked:
        passed.append("price: valuation checked")
    else:
        failed.append("price: valuation not checked (no filing data yet)")

    total = context.total_value_eur
    for theme in context.themes_by_ticker.get(ticker, ()):
        weight = (
            context.theme_values_eur.get(theme, 0.0) / total * 100 if total > 0 else 0.0
        )
        if weight >= CONCENTRATION_ALERT_PCT:
            failed.append(
                f"fit: theme {theme} is already {weight:.1f}%, at or over the "
                f"{CONCENTRATION_ALERT_PCT:.0f}% alert level"
            )
        else:
            passed.append(f"fit: theme {theme} at {weight:.1f}%")

    return BuyChecks(failed=tuple(failed), passed=tuple(passed))


def theme_headroom(ticker: str, context: GuardrailContext) -> float | None:
    """Return the most EUR that keeps every theme of *ticker* under the alert.

    Buying moves cash into a holding without changing the total, so headroom is
    measured against the total as it stands.

    Args:
        ticker: Holding or security to buy.
        context: Portfolio state to check against.

    Returns:
        The headroom in EUR, or None when the ticker carries no theme.
    """
    themes = context.themes_by_ticker.get(ticker, ())
    if not themes:
        return None
    if context.total_value_eur <= 0:
        return 0.0
    ceiling = CONCENTRATION_ALERT_PCT / 100 * context.total_value_eur
    return max(
        0.0, min(ceiling - context.theme_values_eur.get(theme, 0.0) for theme in themes)
    )


def sell_path(ticker: str, context: GuardrailContext) -> SellPath | None:
    """Return the path that permits selling *ticker*, or None when none does.

    Args:
        ticker: Holding to sell.
        context: Portfolio state to check against.

    Returns:
        The first path that applies, most permissive first.
    """
    status = context.thesis_status_by_ticker.get(ticker, "unexamined")
    conviction = context.conviction_by_ticker.get(ticker)

    if status == "broken":
        return SellPath("reason broke", exit_allowed=True)
    if conviction == "none" and status != "unexamined":
        return SellPath("examined, no reason", exit_allowed=True)
    if status == "deteriorating":
        return SellPath("reason weakened", exit_allowed=False)

    value = context.values_by_ticker.get(ticker, 0.0)
    excess = value - context.total_value_eur * MAX_POSITION_WEIGHT_PCT / 100
    if excess > TRIM_CONTRIBUTION_MONTHS * context.monthly_contribution_eur:
        return SellPath("too big", exit_allowed=False, max_eur=excess)
    return None


def valuation_evidence(conn: sqlite3.Connection) -> frozenset[str]:
    """Return tickers whose valuation has been checked against their history.

    Nothing supplies this yet: it needs dated financial statements, and the
    research pipeline retrieves news, not filings. Returning nothing makes the
    price check fail by name, so a buy is refused as "valuation not checked"
    rather than recommended without one. Filing retrieval replaces this.

    Args:
        conn: Open database connection.

    Returns:
        An empty set, until an evidence source exists.
    """
    return frozenset()
