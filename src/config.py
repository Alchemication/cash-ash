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
