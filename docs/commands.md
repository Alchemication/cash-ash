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
uv run python main.py research         # deep pass on what triage selected
uv run python main.py recommend        # propose actions, rules applied
uv run python main.py decide           # list and record your decisions
uv run python main.py weekly           # the whole cycle, in one command
uv run python main.py report           # the weekly review
uv run python main.py telegram-setup   # register the bot command menu
uv run python main.py daemon           # listen for button presses
uv run python main.py daemon install   # run the listener under launchd
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
uv run python main.py research AMD            # deep pass on one holding
uv run python main.py decide 3 approve --note "agreed, buying Monday"
uv run python main.py decide 3 reject
uv run python main.py report --telegram       # send it to your phone
uv run python main.py weekly --telegram       # run everything and send it
uv run python main.py weekly --skip-research  # everything but the deep passes
uv run python main.py weekly install          # schedule it for Sundays
uv run python main.py weekly stop             # unschedule it
uv run python main.py daemon stop
uv run python main.py daemon restart
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

## Deep research

`research` runs a plan, gathers evidence, answers the planned questions and
judges whether the thesis still holds. With no ticker it researches whatever
the last triage selected.

Planning is separate from answering on purpose. A generic question list would
be identical for every company in every week and worth nothing; the planner
works outward from *this* thesis — the conditions the owner said would change
their mind, the questions left open when it was written, what it assumes
without examining. It also states what it is deliberately leaving alone, since
deciding something can be ignored is part of the job.

**Research proposes; it never adopts.** When a pass concludes the thesis has
weakened, improved or broken, it records a *proposed* revision beside the
active one and stops. The thesis is a record of what you believe, so a pipeline
able to rewrite it would be editing the baseline it is measured against. You
review and accept.

`broken` means a condition you wrote down has actually occurred — not that the
news was bad or the price fell. The prompt is explicit that an absence of
evidence is reported as `unchanged` rather than turned into a verdict.

## Recommendations

`recommend` proposes actions for the week. The model proposes; deterministic
rules in `src/guardrails.py` decide, in code, afterwards. That is the reason a
model is allowed near this decision at all — a prompt asking it to respect a
position limit is a request, and this is not.

Three kinds of rule are enforced:

- **Position caps.** An ADD that would push a holding past the weight limit is
  reduced to the largest amount that stays under it, solved properly rather
  than approximated — buying raises both the holding and the portfolio total.
- **Capital.** Single-trade limit, weekly allocation, and cash plus the
  planned contribution. The allocation is consumed across proposals within one
  run, so three recommendations cannot each spend the same money.
- **Sell discipline, taken from your own strategy file.** EXIT and TRIM require
  a thesis that has actually deteriorated or broken, or a position that has
  grown too large. A proposal to sell because a price fell is refused, not
  argued with. An EXIT on an oversized position whose thesis is intact is
  reduced to a TRIM, because being too large justifies trimming and never
  closing.

Refusals are shown rather than hidden. "The model wanted to sell and the rules
would not let it" is a different event from "the model recommended nothing",
and the two must not look alike.

Every recommendation records the price and FX rate that stood behind it. That
is the forward-tracking every later evaluation depends on, and it cannot be
reconstructed after the fact.

## Decisions and execution

`decide` records approve, reject or later. **Approving is not executing** —
the gap is deliberate, because your strategy asks for a cooling-off period, and
collapsing the two would erase the evidence of whether you acted at all.

Recommendations expire. A weekly cadence supersedes itself, so acting on a
stale one would execute research that has already been replaced, at a price
that has moved. `decide` refuses an expired recommendation and says why.

## The weekly run

```bash
uv run python main.py weekly            # sync, triage, research, decide, report
uv run python main.py weekly install    # schedule it
```

One command, because five driven by hand is how a weekly habit fails to form.

**Stages degrade rather than abort.** A failed price sync leaves yesterday's
prices and the run continues on them, marked stale. A research pass that fails
on one holding does not stop the others. A failed decision still leaves a
report to send. Abandoning the run on the first fault turns a partial answer
into no answer, and the next attempt is seven days away.

Deep research is capped, four by default. It is the expensive stage and the
slow one — roughly eight minutes a holding — and anything triage selected
beyond the cap waits a week rather than making the run enormous.

Scheduled for Sunday evening: the week's news has landed, markets are shut so
nothing moves mid-run, and there is a day before Monday's open to think about
anything proposed. The strategy asks for a cooling-off period, and a Sunday
report gives one for free.

The scheduled job and the listener are separate launchd jobs with separate
labels. They have different lifetimes — the listener runs continuously, the
weekly run fires once and exits — and sharing a label would mean stopping one
stops both.

Both are invoked through `uv run` rather than a fixed interpreter path, so a
rebuilt virtual environment or an added dependency does not leave a job
pointing at a stale interpreter that fails only at the next restart. launchd
starts jobs with almost nothing in the environment, so both set `HOME` — every
user-owned path here derives from it — and a `PATH` including the Homebrew
locations, which the default omits entirely on Apple Silicon.

The listener's `KeepAlive` restarts it on a crash but not on a clean exit. With
no bot token there is nothing to listen to, so it logs once and exits; an
unconditional `KeepAlive` would relaunch it into the same misconfiguration
every thirty seconds. Logs go to `~/Library/Logs`, where Console.app looks.

## The weekly report

`report` renders the week: value, anything needing a decision, and what is
standing rather than new. `--telegram` sends it, with Approve / Reject / Later
buttons on anything actionable.

It is written to be read on a phone by someone learning, which shapes it: short,
jargon expanded, and plain about a quiet week. A report that manufactures
content to look useful trains the reader to stop opening it, so when nothing
needs doing it says exactly that and explains why that is the normal outcome.

Holdings with no real reason behind them are reported separately, under
"standing, not new". They are not this week's finding and never will be, and
repeating them as though they were would be the generic-summary habit the whole
design avoids.

One bot serves every profile. The token is shared infrastructure in `.env` and
the per-person part is the numeric `telegram_id` in the roster, so nobody needs
their own BotFather registration.

Sending is retried on a network fault but never on a refusal — a bad chat id or
malformed markup fails identically every time, so retrying only delays the
error. A message carrying buttons is never split, because the buttons would end
up detached from what they act on.

## The listener

Sending needs no daemon. Receiving does, because Telegram delivers updates by
being asked for them and something has to keep asking.

```bash
uv run python main.py daemon           # foreground, Ctrl-C to stop
uv run python main.py daemon install   # under launchd, survives logout
```

`daemon install` writes a launchd job with `KeepAlive`, which is what makes the
listener survive the machine sleeping — a connection dropped while asleep kills
the poll, and without it the daemon would stay dead until noticed by hand. A
crash loop is throttled rather than relaunched as fast as launchd can manage.

Every update is routed by the sender's numeric Telegram id to a profile in the
roster. **An update from an id not in the roster is ignored in silence** — not
answered, not acknowledged. The bot's username is discoverable, and replying
would confirm the bot is live and tell a stranger their id is merely not on the
list. A disabled profile is treated the same way.

Button payloads are matched against a fixed anchored pattern rather than
parsed. `callback_data` arrives from the network and is the one piece of
user-controlled input that reaches a database write.

The update offset is advanced and persisted even when handling an update
raised. A message that crashes the handler would crash it again on every
restart, and a stuck offset means nothing after it is ever seen.

Two pollers steal each other's updates, so button presses would be handled at
random. Telegram signals this with a 409, and the daemon stops rather than
retrying — a failure that would otherwise present as buttons intermittently
doing nothing.

## Evidence and provenance

Two kinds of claim reach an analysis and they are not interchangeable.

A **sourced** claim is anchored to something published and carries a URL and a
publication date. The database refuses to store one without both, so this is a
constraint rather than an instruction a prompt could ignore. Anything
time-sensitive has to be this kind — what a company just reported, how a market
moved, what was announced last week — because that is precisely where a model's
recollection is least reliable and most confident.

A **background** claim is the model's own knowledge: how an industry works,
what happened years ago, what a pattern usually implies. That is genuinely
useful and is allowed. What it may not do is masquerade as a current fact.
Where a stage relies on it, the output says so and tags the claim `unverified`,
so a reader can see the trigger was recollection rather than a dated source.

The default source is free news aggregation needing no key. It supplies
provenance but not quality — it carries retail commentary rather than filings,
so an item establishes that something was said, not that it is true. A paid
search API costs roughly ten dollars a year at this portfolio's usage and is a
plausible upgrade, but adding a credential before the free source has been
shown inadequate is a cost with no measured benefit.

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
