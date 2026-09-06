"""Shared paths, currency settings, and portfolio guardrails.

Every operational tunable lives here: paths under the app home, the accounting
base currency, the seed snapshot's date, and the deterministic constraints the
portfolio decision layer will enforce over LLM recommendations. Nothing is
inlined at its point of use, so this file is the one place to look for what a
value is and why it is that value.

Most values accept a ``SKARBIE_*`` environment override, read once at import.

Example:
    from config import DB_PATH, MAX_POSITION_WEIGHT_PCT
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env_float(name: str, default: float) -> float:
    """Return a float from an environment variable."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return float(raw.strip())


def _env_int(name: str, default: int) -> int:
    """Return an int from an environment variable."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return int(raw.strip())


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

APP_HOME: Path = Path(
    os.environ.get("SKARBIE_HOME", "~/Documents/skarbie")
).expanduser()
"""Root directory for user-owned skarbie state."""

# Per-profile paths — the database, the broker snapshot, the context files —
# are owned by ``profiles.Profile``, not defined here. A path that depends on
# which person a command is acting for cannot be a module-level constant.


PROMPTS_DIR: Path = Path(__file__).resolve().parent / "prompts"
"""Natural-language prompts. Versioned with the code, not with user data:
a prompt is part of what the system is, and a result recorded without
knowing which prompt produced it cannot be attributed."""


# ---------------------------------------------------------------------------
# Currency and accounting
# ---------------------------------------------------------------------------

BASE_CURRENCY: str = "EUR"
"""Currency the book is kept in.

Every trade, cash flow and valuation is stored in EUR, because that is what the
broker reports and what the user actually gains or loses. Prices arrive in each
security's native listing currency and are converted at a stored FX rate, so a
position's return can be decomposed into stock move and FX move rather than
silently blending the two — which is exactly what the broker's own percentage
does, and why it cannot be used as an input to a decision.
"""

SEED_RECONCILIATION_TOLERANCE_EUR: float = _env_float(
    "SKARBIE_SEED_RECONCILIATION_TOLERANCE_EUR", 1.0
)
"""How far the derived book may sit from the broker's stated total before
seeding refuses to proceed.

Cost basis is derived from per-position returns quoted to two decimals, so it
cannot land exactly; the observed error on a 14-position snapshot is under a
cent. A euro is loose enough to absorb rounding on a much larger portfolio and
tight enough that a transposed digit or a missing row cannot slip through.
"""


# ---------------------------------------------------------------------------
# Capital available to allocate
# ---------------------------------------------------------------------------

DEFAULT_MONTHLY_CONTRIBUTION_EUR: float = _env_float(
    "SKARBIE_DEFAULT_MONTHLY_CONTRIBUTION_EUR", 150.0
)
"""Default new money per month for a newly created profile.

Only a default: the live figure is per-profile, in ``profiles.toml``, because
two people in a household do not contribute the same amount.

The portfolio holds almost no cash, so without an expected contribution the
decision layer can only ever emit HOLD/TRIM/EXIT and half the system is
unreachable. This is the *planning* assumption; actual money added is recorded
as a ``cash_flow`` row and is what the allocator is really limited by.

150 is the midpoint of the 100-200 range the user was comfortable with.
"""


# ---------------------------------------------------------------------------
# Portfolio guardrails
#
# Deterministic constraints on what the LLM layer is allowed to propose. Sized
# for a ~EUR 1,400 portfolio of 14 positions, where equal weight is ~7%.
# ---------------------------------------------------------------------------

MAX_POSITION_WEIGHT_PCT: float = _env_float("SKARBIE_MAX_POSITION_WEIGHT_PCT", 20.0)
"""Weight above which ADD is refused regardless of how good the research looks.

Set just above the largest existing position (BRK.B at 19.2% on the seed date)
so the cap binds immediately rather than being decorative, and so the first
thing the system does is refuse to concentrate further into its biggest bet.
"""

LARGE_POSITION_WEIGHT_PCT: float = _env_float("SKARBIE_LARGE_POSITION_WEIGHT_PCT", 12.0)
"""Weight at which a position is flagged 'large' — roughly 1.7x equal weight.

Not a limit. It marks positions where an ADD needs a stronger argument and a
TRIM needs a weaker one.
"""

MAX_NEW_TRADE_EUR: float = _env_float("SKARBIE_MAX_NEW_TRADE_EUR", 100.0)
"""Largest single recommended trade.

At this portfolio size a EUR 100 trade is already a 7% position, so anything
larger is a portfolio-level decision rather than a research conclusion.
"""

MAX_WEEKLY_ALLOCATION_EUR: float = _env_float(
    "SKARBIE_MAX_WEEKLY_ALLOCATION_EUR", 100.0
)
"""Ceiling on new money deployed in one weekly review.

Deliberately above the pro-rata weekly share of the monthly contribution
(~EUR 35): contributions arrive monthly and opportunities do not, so the
allocator is allowed to spend a month's worth in one week. Real available cash
is still the binding constraint; this only stops a single week from committing
several months ahead.
"""

CONCENTRATION_ALERT_PCT: float = _env_float("SKARBIE_CONCENTRATION_ALERT_PCT", 40.0)
"""Theme or sector weight that gets reported as a concentration warning.

On the seed date US mega-cap tech is 53% of the portfolio, so this fires on day
one. That is the intended behaviour — it is the single most useful thing the
system can say before any model is involved.
"""

RECOMMENDATION_EXPIRY_DAYS: int = _env_int("SKARBIE_RECOMMENDATION_EXPIRY_DAYS", 7)
"""How long an unactioned recommendation stays approvable.

A weekly cadence means the next review supersedes the last one. Approving a
stale BUY executes research that has already been replaced, at a price that has
already moved.
"""


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

MARKET_DATA_PROVIDER: str = os.environ.get("SKARBIE_MARKET_DATA_PROVIDER", "yfinance")
"""Which market-data adapter to use.

yfinance by default: free, no key, covers prices and FX in one dependency. It
is scraped rather than licensed and breaks occasionally, which is why it sits
behind an abstraction instead of being called directly.
"""

MARKET_DATA_LOOKBACK: str = os.environ.get("SKARBIE_MARKET_DATA_LOOKBACK", "5d")
"""How much history to request in order to find one usable close.

Not a window of interest — only the most recent close is kept. Five days is
enough to reach back past a long weekend plus a public holiday, which is the
realistic worst case for a Monday-morning sync finding an empty series.
"""

PRICE_STALE_AFTER_DAYS: int = _env_int("SKARBIE_PRICE_STALE_AFTER_DAYS", 4)
"""Age at which a stored price is reported as stale rather than used silently.

A Friday close read on Monday is three days old and perfectly normal; add a
public holiday and it is four. Beyond that the sync has probably been failing,
and valuing a portfolio on a stale price without saying so is how a weekly
review quietly reviews last month.
"""


# ---------------------------------------------------------------------------
# Model routing
#
# Defaults are deliberately cheap. On a ~EUR 1,400 portfolio a weekly research
# run on premium models could cost more per year than the portfolio returns,
# so the strong tier is opt-in per feature and its cost is measured.
# ---------------------------------------------------------------------------

FLASH_MODEL: str = os.environ.get("SKARBIE_FLASH_MODEL", "zai/glm-5.3-flash")
"""Default model for every feature. Cheap, fast, and always reasoning.

Roughly USD 0.15 per million input tokens and 0.50 per million output. A full
weekly pass over fourteen holdings costs a few cents at this rate, which is
what makes running the whole pipeline every week defensible at all.
"""

FAST_MODEL: str = os.environ.get("SKARBIE_FAST_MODEL", "openai/gpt-5.6-luna")
"""Non-reasoning model on a second provider, used as the fallback.

Its value here is redundancy, not capability. Being on a different provider
means an outage at the primary is survivable rather than merely a different
model failing the same way, and emitting no reasoning tokens means it cannot
hit the truncation trap the primary is prone to.

Nominally dearer per token than the flash tier (0.20/1.20 against 0.15/0.50 per
million). It is not a cheaper substitute for analysis: on a task where the
reasoning is the product, the reasoning is what you are paying for. Which model
is actually better at research is an evaluation question, not a pricing one.
"""

PRO_MODEL: str = os.environ.get("SKARBIE_PRO_MODEL", "zai/glm-4.7")
"""Stronger model, roughly four times the price of the flash tier.

Not used by any feature by default. Route a feature here with `main.py models`
when there is a measured reason to, not on the assumption that a bigger model
must be better at a task nobody has evaluated yet.
"""

MAX_TOKENS_DEFAULT: int = _env_int("SKARBIE_MAX_TOKENS_DEFAULT", 4000)
"""Default output budget for a model call."""

THESIS_MAX_TOKENS: int = _env_int("SKARBIE_THESIS_MAX_TOKENS", 9000)
"""Output budget for a thesis restatement.

Measured rather than chosen. At 3,000 tokens, eight of fourteen bootstrap calls
hit finish_reason 'length' and had to be retried at a larger budget — paying
for each of those twice and roughly doubling the wall-clock time. The call is
given the owner's whole log plus their strategy and investor notes, and the
model reasons over all of it before writing a few hundred tokens of answer, so
the budget has to cover the thinking rather than the output.

Generous on purpose: an unused budget costs nothing, since billing is on tokens
produced, while too small a budget costs the entire call and then the retry.
"""

MIN_MAX_TOKENS: int = _env_int("SKARBIE_MIN_MAX_TOKENS", 1024)
"""Floor under any output budget, enforced rather than merely defaulted.

Measured, not guessed: GLM-5.3-Flash always reasons and cannot be told not to.
Asked a one-sentence question with a 400-token budget it spent all 400 on
reasoning and returned empty content with finish_reason 'length' — an answer
that costs money and contains nothing. Any budget small enough to be consumed
entirely by reasoning is a bug, so callers cannot set one.
"""

TRUNCATION_RETRY_MULTIPLIER: float = _env_float(
    "SKARBIE_TRUNCATION_RETRY_MULTIPLIER", 2.5
)
"""Budget multiplier when a reply is truncated before any content appeared.

A reasoning model that ran out of budget mid-thought produces empty content
rather than a short answer, so retrying at the same size repeats the failure
and pays twice. Retried once only; a second truncation means the prompt is
asking for too much, which more budget will not fix.
"""

LLM_RETRY_DELAYS: tuple[int, ...] = (5, 15, 45)
"""Backoff between retries of a transient failure, in seconds.

Three attempts spanning about a minute. A weekly batch job can afford to wait;
what it cannot afford is to abandon a run because one provider blipped.
"""

LLM_TIMEOUT_S: float = _env_float("SKARBIE_LLM_TIMEOUT_S", 180.0)
"""Per-request timeout. Generous because reasoning models are slow, and a
research call that takes two minutes is still cheaper than a failed run.
"""
