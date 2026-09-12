"""Shared paths, currency settings, and portfolio guardrails.

Every operational tunable lives here: paths under the app home, the accounting
base currency, the seed snapshot's date, and the deterministic constraints the
portfolio decision layer will enforce over LLM recommendations. Nothing is
inlined at its point of use, so this file is the one place to look for what a
value is and why it is that value.

Most values accept a ``CASH_ASH_*`` environment override, read once at import.

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
    os.environ.get("CASH_ASH_HOME", "~/Documents/cash-ash")
).expanduser()
"""Root directory for user-owned CashAsh state."""

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
    "CASH_ASH_SEED_RECONCILIATION_TOLERANCE_EUR", 1.0
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
    "CASH_ASH_DEFAULT_MONTHLY_CONTRIBUTION_EUR", 150.0
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

MAX_POSITION_WEIGHT_PCT: float = _env_float("CASH_ASH_MAX_POSITION_WEIGHT_PCT", 20.0)
"""Weight above which ADD is refused regardless of how good the research looks.

Set just above the largest existing position (BRK.B at 19.2% on the seed date)
so the cap binds immediately rather than being decorative, and so the first
thing the system does is refuse to concentrate further into its biggest bet.
"""

LARGE_POSITION_WEIGHT_PCT: float = _env_float(
    "CASH_ASH_LARGE_POSITION_WEIGHT_PCT", 12.0
)
"""Weight at which a position is flagged 'large' — roughly 1.7x equal weight.

Not a limit. It marks positions where an ADD needs a stronger argument and a
TRIM needs a weaker one.
"""

MAX_NEW_TRADE_EUR: float = _env_float("CASH_ASH_MAX_NEW_TRADE_EUR", 100.0)
"""Largest single recommended trade.

At this portfolio size a EUR 100 trade is already a 7% position, so anything
larger is a portfolio-level decision rather than a research conclusion.
"""

MAX_WEEKLY_ALLOCATION_EUR: float = _env_float(
    "CASH_ASH_MAX_WEEKLY_ALLOCATION_EUR", 100.0
)
"""Ceiling on new money deployed in one weekly review.

Deliberately above the pro-rata weekly share of the monthly contribution
(~EUR 35): contributions arrive monthly and opportunities do not, so the
allocator is allowed to spend a month's worth in one week. Real available cash
is still the binding constraint; this only stops a single week from committing
several months ahead.
"""

CONCENTRATION_ALERT_PCT: float = _env_float("CASH_ASH_CONCENTRATION_ALERT_PCT", 40.0)
"""Theme or sector weight that gets reported as a concentration warning.

On the seed date US mega-cap tech is 53% of the portfolio, so this fires on day
one. That is the intended behaviour — it is the single most useful thing the
system can say before any model is involved.
"""

RECOMMENDATION_EXPIRY_DAYS: int = _env_int("CASH_ASH_RECOMMENDATION_EXPIRY_DAYS", 7)
"""How long an unactioned recommendation stays approvable.

A weekly cadence means the next review supersedes the last one. Approving a
stale BUY executes research that has already been replaced, at a price that has
already moved.
"""


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

MARKET_DATA_PROVIDER: str = os.environ.get("CASH_ASH_MARKET_DATA_PROVIDER", "yfinance")
"""Which market-data adapter to use.

yfinance by default: free, no key, covers prices and FX in one dependency. It
is scraped rather than licensed and breaks occasionally, which is why it sits
behind an abstraction instead of being called directly.
"""

MARKET_DATA_LOOKBACK: str = os.environ.get("CASH_ASH_MARKET_DATA_LOOKBACK", "5d")
"""How much history to request in order to find one usable close.

Not a window of interest — only the most recent close is kept. Five days is
enough to reach back past a long weekend plus a public holiday, which is the
realistic worst case for a Monday-morning sync finding an empty series.
"""

PRICE_STALE_AFTER_DAYS: int = _env_int("CASH_ASH_PRICE_STALE_AFTER_DAYS", 4)
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

FLASH_MODEL: str = os.environ.get("CASH_ASH_FLASH_MODEL", "zai/glm-5.3-flash")
"""Default model for every feature. Cheap, fast, and always reasoning.

Roughly USD 0.15 per million input tokens and 0.50 per million output. A full
weekly pass over fourteen holdings costs a few cents at this rate, which is
what makes running the whole pipeline every week defensible at all.
"""

FAST_MODEL: str = os.environ.get("CASH_ASH_FAST_MODEL", "openai/gpt-5.6-luna")
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

PRO_MODEL: str = os.environ.get("CASH_ASH_PRO_MODEL", "zai/glm-4.7")
"""Stronger model, roughly four times the price of the flash tier.

Not used by any feature by default. Route a feature here with `main.py models`
when there is a measured reason to, not on the assumption that a bigger model
must be better at a task nobody has evaluated yet.
"""

MAX_TOKENS_DEFAULT: int = _env_int("CASH_ASH_MAX_TOKENS_DEFAULT", 4000)
"""Default output budget for a model call."""

THESIS_MAX_TOKENS: int = _env_int("CASH_ASH_THESIS_MAX_TOKENS", 9000)
"""Output budget for a thesis restatement.

Measured rather than chosen. At 3,000 tokens, eight of fourteen bootstrap calls
hit finish_reason 'length' and had to be retried at a larger budget — paying
for each of those twice and roughly doubling the wall-clock time. The call is
given the owner's whole log plus their strategy and investor notes, and the
model reasons over all of it before writing a few hundred tokens of answer, so
the budget has to cover the thinking rather than the output.

Generous on purpose: an unused budget costs nothing, since billing is on tokens
produced, while too small a budget costs the entire call and then the retry.

Covers both `plan` calls — the bootstrap restatement and the research plan
derived from a thesis. Triage and the analyst pass have their own budgets,
because they reason over the whole book and over a security's evidence
respectively, and neither is bounded by the size of a thesis.
"""

MIN_MAX_TOKENS: int = _env_int("CASH_ASH_MIN_MAX_TOKENS", 1024)
"""Floor under any output budget, enforced rather than merely defaulted.

Measured, not guessed: GLM-5.3-Flash always reasons and cannot be told not to.
Asked a one-sentence question with a 400-token budget it spent all 400 on
reasoning and returned empty content with finish_reason 'length' — an answer
that costs money and contains nothing. Any budget small enough to be consumed
entirely by reasoning is a bug, so callers cannot set one.
"""

DECISION_MAX_TOKENS: int = _env_int("CASH_ASH_DECISION_MAX_TOKENS", 20000)
"""Output budget for the portfolio decision.

The largest of any stage, and measured rather than chosen. Deciding across
fourteen holdings at once produced 34,500 characters of reasoning — roughly
8,600 tokens — before the answer began, then ran out mid-object. The budget has
to cover the thinking, the answer, and the margin between them.

An unused budget costs nothing, since billing is on tokens produced. Too small
a budget costs the whole call and then the retry, which on this stage is eight
minutes each time.
"""

TRIAGE_MAX_TOKENS: int = _env_int("CASH_ASH_TRIAGE_MAX_TOKENS", 20000)
"""Output budget for the weekly triage ranking.

Sized like `DECISION_MAX_TOKENS` because it is the same shape of call: triage
ranks the whole book at once, so it reasons over fourteen holdings before
writing anything. Measured — at 9,000 tokens it ran out mid-reasoning and the
truncation retry finished it at 22,500, having produced 8,580. A budget the
task can consume entirely leaves no margin for a fifteenth holding.

Ranking one holding per call would bound the reasoning instead of paying for
it, but that is a prompt change rather than a budget one.
"""

ANALYST_MAX_TOKENS: int = _env_int("CASH_ASH_ANALYST_MAX_TOKENS", 20000)
"""Output budget for one analyst pass over a security's evidence.

Measured, and the measurement is the warning: the only pass to complete at
9,000 tokens produced 8,989 of them. Eleven tokens of headroom means the budget
was binding rather than the task finishing, and the evidence set grows whenever
a profile adds primary excerpts to `context/evidence.json`.

An unused budget costs nothing, since billing is on tokens produced, while a
truncated pass costs the call, then the retry at
`TRUNCATION_RETRY_MULTIPLIER`, then the fallback model.
"""

TRUNCATION_RETRY_MULTIPLIER: float = _env_float(
    "CASH_ASH_TRUNCATION_RETRY_MULTIPLIER", 2.5
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

LLM_TIMEOUT_S: float = _env_float("CASH_ASH_LLM_TIMEOUT_S", 180.0)
"""Per-request timeout. Generous because reasoning models are slow, and a
research call that takes two minutes is still cheaper than a failed run.
"""


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

EVIDENCE_SOURCE: str = os.environ.get("CASH_ASH_EVIDENCE_SOURCE", "yfinance-news")
"""Where the facts an analysis reasons over are retrieved from.

Free news aggregation by default, needing no key. It supplies provenance — a
URL, a date, a publisher — but not quality: it carries retail commentary rather
than filings, so an item establishes that something was said, not that it is
true. A paid search API is a plausible upgrade at roughly ten dollars a year at
this portfolio's usage, but adding a credential before the free source has been
shown inadequate is a cost with no measured benefit.
"""

EVIDENCE_ITEMS_PER_SECURITY: int = _env_int("CASH_ASH_EVIDENCE_ITEMS_PER_SECURITY", 8)
"""How many items to retrieve per security for one research pass.

Enough to see what a week actually contained without burying the thesis in
noise. The aggregator returns around ten, most of which are commentary rather
than news, so raising this mostly buys more opinions about the same events.
"""


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
"""Shared bot token. One bot serves every profile.

People are told apart by the numeric ``telegram_id`` in ``profiles.toml``, not
by having their own bot. A bot each would mean a BotFather registration, a
token and a polling loop per person, for nothing.
"""

TELEGRAM_MAX_MESSAGE_CHARS: int = 3800
"""Chunk size for outgoing messages.

Telegram's hard limit is 4096. The margin absorbs the HTML tags added after a
chunk boundary is chosen, which would otherwise push an already-sized chunk
over and fail the send.
"""

TELEGRAM_RETRY_DELAYS: tuple[int, ...] = (2, 8, 20)
"""Backoff between retries of a failed send.

Only transient faults are retried. A refusal — a bad chat id, malformed HTML —
fails identically every time, so retrying it only delays the error.
"""

TELEGRAM_TIMEOUT_S: float = _env_float("CASH_ASH_TELEGRAM_TIMEOUT_S", 20.0)
"""Per-request timeout for the Telegram API."""


LAUNCHD_LABEL: str = "com.cash-ash.daemon"
"""launchd job label, and the stem of the installed plist filename.

Defined here rather than beside the install command because install, stop
and restart all need the same answer, and three copies of a literal is how
one of them ends up managing a different job."""

DAEMON_POLL_TIMEOUT_S: int = _env_int("CASH_ASH_DAEMON_POLL_TIMEOUT_S", 30)
"""How long each long-poll waits for an update before returning empty.

Long polling rather than repeated short requests: Telegram holds the connection
until something arrives, so a quiet day costs almost nothing and a button press
is handled in about a second.
"""

DAEMON_ERROR_BACKOFF_S: int = _env_int("CASH_ASH_DAEMON_ERROR_BACKOFF_S", 15)
"""Pause after a failed poll before trying again.

Long enough that a provider outage does not become a request flood, short
enough that a button press is not left unanswered for minutes once it clears.
"""


WEEKLY_RUN_WEEKDAY: int = _env_int("CASH_ASH_WEEKLY_RUN_WEEKDAY", 0)
"""Day the weekly run fires, with Sunday as 0.

Sunday by default: the week's news has landed, markets are shut so no price
moves mid-run, and there is a day before Monday's open to think about anything
it proposes. The strategy asks for a cooling-off period, and a Sunday report
gives one for free.
"""

WEEKLY_RUN_HOUR: int = _env_int("CASH_ASH_WEEKLY_RUN_HOUR", 18)
"""Hour the weekly run fires, local time."""


def _log_file() -> Path:
    """Resolve where background jobs write, honouring an override."""
    override = os.environ.get("CASH_ASH_LOG_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Logs" / "cash-ash.daemon.log"


DAEMON_LOG_FILE: Path = _log_file()
"""Where the background jobs write their output.

``~/Library/Logs`` rather than the app home, following the macOS convention
zdrowskit uses: it is where Console.app looks, and it keeps machine output out
of a directory otherwise holding only the user's own files.
"""


SCHEDULED_CHECK_INTERVAL_S: int = _env_int(
    "CASH_ASH_SCHEDULED_CHECK_INTERVAL_S", 30 * 60
)
"""How often the daemon asks whether the week's run has happened.

Half an hour, matching zdrowskit. The check is a state question — has a run
been recorded for this ISO week? — not a clock comparison, so a coarse interval
costs only lateness and never a missed week: a machine asleep at the scheduled
hour runs within half an hour of waking.
"""


BENCHMARK_TICKER: str = os.environ.get("CASH_ASH_BENCHMARK_TICKER", "VWCE.DE")
"""The passive alternative the portfolio is measured against.

A global all-world tracker quoted in EUR, so the comparison needs no currency
conversion and no view on which region should have been held. Choosing a US
index instead would flatter or punish the portfolio for a bet it did not
deliberately make.

The comparison is the only measure of this system that will ever mean much: at
fourteen holdings a week, nothing else has the sample size to distinguish skill
from noise.
"""

BENCHMARK_NAME: str = os.environ.get(
    "CASH_ASH_BENCHMARK_NAME", "FTSE All-World (accumulating)"
)
"""Human-readable name for the benchmark, shown in reports."""


BENCHMARK_MEANINGFUL_AFTER_DAYS: int = _env_int(
    "CASH_ASH_BENCHMARK_MEANINGFUL_AFTER_DAYS", 3 * 365
)
"""How long the benchmark comparison must run before it is worth reading.

Three years. Not a statistical threshold — on one portfolio there is no sample
to compute one from — but the point at which a difference stops being
attributable to which week the money happened to arrive.

It exists so the comparison can say when it does not yet mean anything.
Recording begins in week one because the series cannot be reconstructed later;
reading it as a verdict should not, and a number displayed without that caveat
invites exactly the mistake the system is built to avoid.
"""


RESEARCH_MAX_PASSES: int = _env_int("CASH_ASH_RESEARCH_MAX_PASSES", 4)
"""Weekly deep-pass cap; limits latency and spend while allowing rotation."""

RESEARCH_OVERDUE_DAYS: int = _env_int("CASH_ASH_RESEARCH_OVERDUE_DAYS", 42)
"""Six-week coverage floor so quiet holdings cannot be skipped forever."""

RESEARCH_ROTATION_SLOTS: int = 1
"""Reserve one weekly slot for the oldest coverage gap without crowding out events."""

SNOOZE_DAYS: int = _env_int("CASH_ASH_SNOOZE_DAYS", 2)
"""Two days to reconsider an item while keeping it inside the weekly review."""

EVIDENCE_MAX_AGE_DAYS: int = _env_int("CASH_ASH_EVIDENCE_MAX_AGE_DAYS", 120)
"""Allow the latest quarterly disclosure, but exclude old news from current evidence."""

RESEARCH_ASSET_CLASSES: tuple[str, ...] = ("equity",)
"""The company-thesis analyst supports equities; other instruments require manual review."""

TRIAGE_HORIZON_DAYS: int = 21
"""Three weeks captures upcoming reporting dates without a distant-event backlog."""

TRIAGE_LOOKBACK_DAYS: int = 14
"""Two weeks tolerates one missed review when collecting recent events."""


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

CHAT_SQL_ROW_LIMIT: int = _env_int("CASH_ASH_CHAT_SQL_ROW_LIMIT", 200)
"""Hard cap on rows one chat query may return.

Two hundred is generous for a book of fourteen positions — it is the whole
trade ledger several times over — while still being well inside what fits in a
model's context beside the rest of the prompt. A query that wants more is
asking the wrong question and should aggregate instead.
"""

CHAT_SQL_DEFAULT_ROWS: int = 50
"""Rows returned when a chat query names no limit of its own.

Low enough that an exploratory ``SELECT *`` costs little, and the model can ask
for more once it knows what it is looking at.
"""

CHAT_SQL_TIMEOUT_S: float = _env_float("CASH_ASH_CHAT_SQL_TIMEOUT_S", 5.0)
"""Seconds a single chat query may run before it is abandoned.

The database is a few thousand rows on local disk, so anything slower than this
is a cartesian join rather than honest work, and the person is waiting on it.
"""

CHAT_MAX_TOOL_ITERATIONS: int = 6
"""How many times one chat turn may call tools before it must answer.

Enough for a real drill-down — look at the portfolio, query the trades behind
one holding, check a price, then answer — plus a retry when a query comes back
empty or malformed. Beyond that the loop is not converging, and every extra
iteration resends the whole conversation at the cost of a fresh call.
"""

CHAT_MAX_TOKENS: int = _env_int("CASH_ASH_CHAT_MAX_TOKENS", 2000)
"""Output budget for one chat reply.

A Telegram message is read on a phone, so a long answer is a worse answer. This
is deliberately below the weekly stages' budgets: those produce documents, this
produces a paragraph.
"""

CHAT_CONVERSATION_MESSAGES: int = 20
"""Turns of conversation kept in memory for follow-up questions.

Ten exchanges is more than any real follow-up chain reaches, and the cost of
keeping them is paid on every call — the whole buffer is resent each time.
"""
