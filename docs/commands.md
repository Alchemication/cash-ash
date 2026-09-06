# Commands

Always use `uv run`. Run any command with `--help` for the full flag list.

```bash
uv run python main.py profile add NAME --telegram-id ID --operator   # first profile
uv run python main.py profile list     # the roster
uv run python main.py init             # create and seed from the snapshot file
uv run python main.py sync             # prices, FX, earnings dates, consensus
uv run python main.py events           # known upcoming dates for your holdings
uv run python main.py thesis           # why each position is held
uv run python main.py triage           # rank holdings by what changed
uv run python main.py models           # which model each stage calls
uv run python main.py llm-log          # recorded model calls and their cost
uv run python main.py price TICKER --close AMOUNT   # record a price by hand
uv run python main.py holdings         # positions, cost basis, value, P&L
uv run python main.py concentration    # grouped weights and limit breaches
uv run python main.py context          # personal context files and their status
uv run python main.py doctor           # what is set up and what is missing
uv run python main.py db status        # row counts and migration state
uv run python main.py db migrate       # apply pending migrations
uv run python main.py db schema        # print the live schema
```

Useful examples:

```bash
uv run python main.py init --dry-run          # verify the snapshot reconciles, write nothing
uv run python main.py init --snapshot ./other.toml   # seed from a different file
uv run python main.py concentration --by theme
uv run python main.py concentration --by sector
uv run python main.py holdings --verbose      # debug logging on stderr
uv run python main.py sync --provider yfinance
uv run python main.py price SPCX --close 147.95 --date 2026-09-04
uv run python main.py sync --prices-only      # skip the calendar fetch
uv run python main.py events --days 30
uv run python main.py events --past           # include dates already passed
uv run python main.py models set synthesis --model zai/glm-4.7
uv run python main.py models reset all
uv run python main.py models cost --since 2026-09-01
uv run python main.py llm-log --id 42         # one call in full
uv run python main.py llm-log --trace 7       # every call in one operation
uv run python main.py llm-log --errors        # only attempts that failed
uv run python main.py thesis bootstrap        # theses from your own notes
uv run python main.py thesis bootstrap NKE --overwrite
uv run python main.py thesis show NKE
uv run python main.py thesis list
uv run python main.py triage --dry-run        # show the input, send nothing
```

## Profiles

Every portfolio command is profile-scoped. `--profile NAME` selects one;
omitting it means the operator profile in `profiles.toml`. One person is one
profile: their own database, broker snapshot, context files and Telegram id.

```bash
uv run python main.py holdings --profile kasia
uv run python main.py init --profile kasia
```

A single Telegram bot serves everyone. The token is shared infrastructure in
`.env`; the per-person part is the numeric `telegram_id` in the roster, which
the bot routes incoming messages by.

`--db PATH` overrides the database on any command and bypasses the roster
entirely — it exists for experimental databases. Only `init` creates a
database; everything else fails with a message pointing at it, so a typo in a
path or a profile name cannot silently produce an empty portfolio.

## Context files

Each profile owns personal markdown files under
`$SKARBIE_HOME/profiles/<name>/context/`. `profile add` writes templates;
`context` reports which are still templates and which you have written.

Nothing reads them yet — the research pipeline will, from Phase 4. They exist
now so `strategy.md` can be filled in over time rather than in a rush when the
pipeline lands. An unedited template counts as unwritten, because placeholder
prose read as intent is worse than no file at all.

## Market data

`sync` fetches one close per held security and one rate per currency they are
quoted in, then stores both. It asks only about securities you actually hold —
pricing something you sold costs a request and changes no figure.

Prices are stored in the security's own currency and converted at the stored FX
rate, so a EUR return can be separated into stock move and currency move.
Yahoo's `USDEUR=X` is requested directly rather than inverting `EURUSD=X`,
because inverting is where direction errors live and an FX bug misprices every
holding at once.

Anything the feed cannot price is reported, not guessed at. Use `price` to
enter one by hand:

```bash
uv run python main.py price SPCX --close 147.95
```

The provider is unofficial and occasionally breaks. When a request fails
outright, stored prices are left alone and `holdings` keeps using the last ones
it had — marked stale, so a failing sync is visible rather than silent.

## Events

`sync` records three kinds of date, and `events` lists what is coming.

**Feed** — earnings, ex-dividend and dividend dates, fetched automatically. If
a company reschedules, the future date is replaced rather than added beside the
old one; past dates are never touched, because a thesis that referenced one
must still make sense.

**Curated** — everything no feed carries, and often what actually moves a
holding: a product keynote, an IPO lockup expiry, a quarterly delivery report,
a court date, a rate decision. These live in
`$SKARBIE_HOME/profiles/<name>/events.toml`; `events.example.toml` in the
project root shows the format. `sync` imports the file and reports any entry it
had to skip, so a typo surfaces rather than silently losing a date.

**Research** — written by the pipeline when a model finds a date. Deliberately
the least trusted of the three.

Each entry is `confirmed` or `estimated`. An inferred date — a lockup expiry
calculated from a listing date, a keynote in its usual slot — is marked `~` in
the listing so it is never mistaken for something the company announced.

## Consensus estimates

`sync` also records analyst EPS and revenue expectations, keyed by the date
they were *observed* rather than the period they forecast. The provider reports
what consensus is today and never what it was last month, so a revision is only
detectable by comparing today's figure against one already stored. The series
cannot be backfilled, which is why recording starts before anything reads it.

## Theses

A thesis records why a position is held, and — the part that makes it useful —
what would prove it wrong. A statement that cannot be falsified is a
preference, not a thesis, and nothing downstream can detect that it stopped
being true.

`thesis bootstrap` builds the first version of each from your own notes in
`context/log.md`. It is a restatement, not research: the model is instructed to
use only what you wrote, not to strengthen a weak reason, and not to soften a
bad one. That matters because your real reasons are the baseline every later
comparison is made against — a thesis you never held cannot break, and cannot
teach you anything.

Theses are versioned and never edited. A revision is a new version and the old
one is kept, so a year later it is still possible to ask what was believed at
the time and how it changed.

Conviction is an ordinal label — `none`, `weak`, `moderate`, `strong` — and
never a number. An LLM's stated "confidence: 91%" is not a calibrated
probability, and storing it as one would launder a guess into a statistic.
Conviction describes the strength of the *reason*, not the quality of the
company: a great business held for no articulated reason is `weak`.

## Triage

`triage` ranks every holding by how likely it is that something changed which
affects its thesis — not by how much the price moved or how large the position
is. It is one call covering the whole portfolio rather than one per holding,
because the judgement is comparative and is both cheaper and better made once
with everything visible.

Every holding is recorded, including the ones passed over. "Nothing needed
looking at this week" is a finding, and it is invisible if only the selected
holdings are stored. A week with nothing selected is a normal outcome.

The model is told explicitly what it is *not* being given. Absent data and
unchanged data look identical in the rendering, so without that a model reports
calm it never observed — in the first weeks there is no price history and only
one consensus observation, and neither means nothing moved.

Two failure modes are handled rather than hidden. A holding the model omits is
recorded as unranked, because a silently dropped holding looks exactly like one
considered and passed over. And if nothing at all was ranked, the run fails
loudly instead of reporting a triage that considered nothing as though it had
run.

`--dry-run` prints exactly what would be sent and calls no model.

## Model routing

Each stage of the pipeline routes independently, so one expensive step does not
drag the whole run's cost with it, and a change is attributable to the stage it
was made in.

Every stage defaults to the flash tier. The pro tier is opt-in per feature:

```bash
uv run python main.py models                                  # what is in force
uv run python main.py models set synthesis --model zai/glm-4.7
uv run python main.py models cost                             # what it actually cost
```

Three tiers ship. `flash` and `pro` reason before answering; `fast` does not,
and sits on a second provider. `fast` is the default fallback for that reason —
a fallback within one provider survives a bad model but not an outage, which is
the failure that would take a whole weekly run with it. It is redundancy, not a
cheaper way to do the analysis: where the reasoning is the output, the
reasoning is what is being paid for.

Preferences persist per profile, so two people can route differently. Any
litellm model id is accepted, which is how a further provider joins without a
code change.

Analysts default to temperature 0. Disagreement between them should come from
using different models, not from sampling noise, or a rerun cannot tell the two
apart.

## Model call log

Every attempt is recorded, successful or not, with the model, prompt version,
token counts, cost and the reasoning the model emitted. Failures are kept
deliberately: a model that fails repeatedly is exactly what a later evaluation
needs to see, and it is invisible if only successes are stored.

```bash
uv run python main.py llm-log            # recent calls
uv run python main.py llm-log --id 42    # prompt, reasoning, response, cost
uv run python main.py llm-log --trace 7  # every call in one weekly run
```

A trace groups the calls of one operation. Reading a synthesis in isolation
says little about why it concluded what it did; reading it beside the analyst
calls it consumed says a lot.

Two behaviours worth knowing, both measured rather than assumed. Output budgets
have an enforced floor: the default model always reasons and cannot be told not
to, so too small a budget is spent entirely on thinking and returns empty
content that still costs money. And when a reply is truncated before any
content appears, the budget is enlarged once and the fallback model is tried at
that same enlarged size — a terser model can finish where a discursive one
never stops thinking.

## Reading the output

`holdings` values each position at the best price available: a stored market
price converted at the stored FX rate, else the per-unit value implied by the
most recent snapshot, else unknown. The footer names the dates the prices came
from, flags any older than the staleness threshold in `src/config.py`, and
lists anything that could not be priced. Unpriced holdings are excluded from the
total and from every weight.

`concentration` reports three groupings. Security weights are checked against
the position limit; sector and theme weights against the concentration alert
level. Both limits are named in the table title and defined in `src/config.py`.

Theme weights overlap deliberately — NVDA is both a mega-cap and an AI
semiconductor — so they sum past 100%. Splitting a holding's value between its
themes would understate every one of them.

## Seeding

`init` reads a broker snapshot from `$SKARBIE_HOME/seed_snapshot.toml`. That
file holds real holdings and euro amounts, so it lives beside the database and
never in the repository; `seed_snapshot.example.toml` in the project root shows
the format with invented figures.

Seeding is idempotent. Securities are upserted and the snapshot replaces any
earlier one for the same date and source, so a mislabelled sector or theme is
fixed by editing the file and re-running — not by rebuilding the database.
Opening trades are written only when the ledger is empty, so re-running after
real trades exist cannot double the book.

`init` refuses to write when the derived total drifts from the snapshot's
`reported_total_eur` by more than the tolerance in `src/config.py`. Omitting
`reported_total_eur` skips that check and says so.
