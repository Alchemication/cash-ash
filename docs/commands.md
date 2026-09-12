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
uv run python main.py benchmark        # against the same money in a tracker
uv run python main.py eval             # is the machinery sound?
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
uv run python main.py research AMD --no-store # run it for real, keep nothing
uv run python main.py recommend --no-store    # the same, for the decision stage
uv run python main.py decide 3 approve --note "agreed, buying Monday"
uv run python main.py decide 3 reject
uv run python main.py report --telegram       # send it to your phone
uv run python main.py weekly --telegram       # run everything and send it
uv run python main.py weekly --skip-research  # everything but the deep passes
uv run python main.py benchmark sync          # fetch the tracker's history
uv run python main.py daemon install          # one job: listens and schedules
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
`$CASH_ASH_HOME/profiles/<name>/context/`. `profile add` writes templates;
`context` reports which are still templates and which you have written.

Thesis bootstrap reads written `log.md`, `strategy.md` and `investor.md` to
restate the owner's reasons. `recommend` rereads `strategy.md` and `investor.md`
on each run for the owner's horizon, cash needs and restrictions. It does not
read the full log or watchlist. Missing, blank and template files supply no
intent; decision inputs mark absent context explicitly. Personal context sent
to a model is included in that call's stored log.

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
`$CASH_ASH_HOME/profiles/<name>/events.toml`; `events.example.toml` in the
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
The prompt permits empty assumption and breaking-condition lists. It restates
explicit conditions or the direct negation of an explicit reason, without
inventing thresholds or deadlines. Missing conditions become open questions;
inspect the bootstrap with `main.py thesis show` before relying on it. An
empty breaking-condition list is a debt the owner carries, not one the model
pays: `eval` lists such theses as broken invariants until you write what would
change your mind in `context/log.md` and rerun `thesis bootstrap TICKER
--overwrite`.

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
The prompt does not infer normal volatility, market divergence or the cause of
a move from two stored closes. A thin thesis can warrant research without news;
routine overdue coverage is also handled by the research rotation.

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
Planner and analyst receive the research date. The analyst also receives the
owner's full rationale and assumptions, and is asked to identify contradictions
in supplied evidence without manufacturing an opposing case. Planner source
suggestions do not drive automated retrieval; the default feed remains ticker
news, with question-linked local excerpts available for targeted evidence.

**Research proposes; it never adopts.** When a pass concludes the thesis has
weakened, improved or broken, it records a *proposed* revision beside the
active one and stops. The thesis is a record of what you believe, so a pipeline
able to rewrite it would be editing the baseline it is measured against. You
review and accept.

`broken` means a condition you wrote down has actually occurred — not that the
news was bad or the price fell. The prompt is explicit that an absence of
evidence is reported as `unchanged` rather than turned into a verdict.

## Watching a stage without changing anything

`--no-store` runs `research` or `recommend` exactly as the weekly cycle does —
the same prompts, the same models, the same billing — against a copy of the
database that is discarded when the command ends. Nothing is written except the
model-call log, which is kept because the money was spent either way and
`llm-log` is where that is accounted for. The log survives a run that fails
partway, which is when it matters most.

```bash
uv run python main.py research AMD --no-store
uv run python main.py recommend --no-store
```

It exists because observing these two stages otherwise changes them. A research
pass writes a run, its evidence and an assessment; `recommend` supersedes every
recommendation you have not yet approved or rejected. Both are right for the
weekly cycle and wrong for working out what the machinery is doing.

`--model` routes one `--no-store` run differently, so the same stage can be run
on another model and the two compared in `llm-log`:

```bash
uv run python main.py research AMD --no-store --model analyst=zai/glm-4.7
uv run python main.py research AMD --no-store --model plan=zai/glm-4.7,analyst=zai/glm-4.7
```

Features are the stages `main.py models` lists. It is refused without
`--no-store`: a stored result must always match the routing table, or that table
stops explaining how a recorded result was produced. Use `models set` to change
a real route.

## Recommendations

`recommend` proposes actions for the week. The model proposes; deterministic
rules in `src/guardrails.py` decide, in code, afterwards. That is the reason a
model is allowed near this decision at all — a prompt asking it to respect a
position limit is a request, and this is not.

Decision inputs include the latest stored native close with currency, date and
source, FX into EUR with its date and source, available funded cash, reservations
and pending executions. A close is not an executable broker quote. Financial
statements, valuation metrics and transaction-cost estimates are not fetched for
this stage; the model must identify material missing inputs rather than invent
them. Known owner constraints supplement the deterministic rules and cannot
relax them.

The prompt requires a reason to act now rather than a target level of activity.
New funded cash can justify reconsidering an unchanged thesis, but cannot by
itself justify a purchase. BUY/ADD must explain current valuation assumptions,
relevant contrary evidence, material gaps and the comparison with keeping cash.
REVIEW identifies whether resolution needs owner input, thesis adoption or more
evidence. Purchase price and a desire to recover a loss do not justify a trade.

The deterministic checks enforce:

- **Funded cash.** Planned monthly contributions are informational. Approved
  buys reserve cash until execution or rejection; current-week executions and
  outstanding approvals consume the weekly allocation across reruns.
- **Position caps.** Buying transfers cash into securities without increasing
  total wealth. The full batch is checked cumulatively. Duplicate or conflicting
  ticker proposals invalidate a batch before earlier advice is retired.
- **Sell discipline and sizing.** Sales require a priced holding. TRIM is limited
  by holding value and the single-trade cap. An EXIT requires a broken thesis;
  deterioration reduces it to TRIM. An intact oversized position may only be
  trimmed back toward the position cap. EXIT records the full holding value.
- **Evidence and freshness.** Missing/stale prices or FX block trades. BUY, ADD
  and EXIT require a recent assessment with sufficient cited coverage. Failed
  or deferred weekly stages block trade proposals while still allowing REVIEW.
  Citation presence is not a calibrated measure of investment quality.

Refusals are stored and shown in the weekly report. "The model wanted to sell and the rules
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
uv run python main.py daemon install    # and have it run itself, weekly
```

One command, because five driven by hand is how a weekly habit fails to form.

Scheduling lives inside the daemon rather than in a second launchd job. There
is one background process with two threads — a scheduler and the Telegram
listener — following zdrowskit: one thing to install, one log, one thing that
can be broken. Two jobs meant two ways to be half-working, with the listener
running while the schedule was silently absent.

The scheduler asks a question about state — has a run been recorded for this
ISO week? — rather than watching for a moment to pass. A machine asleep at the
scheduled hour therefore runs on waking instead of skipping the week, and a
daemon restarted twice in an hour does not run twice.

It also records what the portfolio was worth, right after refreshing prices.
That value is derivable from dated trades and dated prices, so the snapshot is
not the only record — but a derived figure silently changes when a price is
later corrected, while a snapshot pins what was actually reported at the time,
and it turns "when did this diverge from the index" into a query rather than a
reconstruction. Unpriced holdings are omitted from it rather than recorded as
zero, with the count kept in the snapshot's note.

**Stages degrade rather than abort.** A failed price sync leaves yesterday's
prices and the run continues on them, marked stale. A research pass that fails
on one holding does not stop the others. A failed decision still leaves a
report to send. Abandoning the run on the first fault turns a partial answer
into no answer, and the next attempt is seven days away.

`RESEARCH_MAX_PASSES` caps deep passes; `--max-research` overrides it for one
cycle. `RESEARCH_ROTATION_SLOTS` reserves capacity for holdings older than
`RESEARCH_OVERDUE_DAYS`, ordered by oldest completed assessment. Remaining slots
follow triage priority; unused capacity checks additional overdue holdings.
`main.py process` prints the effective limits. Deferred
selections and unsupported instruments are reported explicitly. Company research
supports `RESEARCH_ASSET_CLASSES`; other asset classes require manual review.

Scheduled for Sunday evening: the week's news has landed, markets are shut so
nothing moves mid-run, and there is a day before Monday's open to think about
anything proposed. The strategy asks for a cooling-off period, and a Sunday
report gives one for free.

The single job is invoked through `uv run` rather than a fixed interpreter path, so a
rebuilt virtual environment or an added dependency does not leave a job
pointing at a stale interpreter that fails only at the next restart. launchd
starts jobs with almost nothing in the environment, so both set `HOME` — every
user-owned path here derives from it — and a `PATH` including the Homebrew
locations, which the default omits entirely on Apple Silicon.

`KeepAlive` restarts the job on a crash but not on a clean exit. With no bot
token there is nothing to listen for, so the listener does not start — but the
scheduler still does, because a weekly review is worth having even when there
is nowhere to send it, and everything it produces is readable from the CLI.
Logs go to `~/Library/Logs`, where Console.app looks.

## Process checks

`eval` reports two different kinds of thing, and keeps them apart on purpose.

**Invariants** are statements that should never be true. An active thesis with
nothing that would break it. A claim marked sourced without a URL and a date. A
decision recorded against advice a later run withdrew. An active thesis the
owner never accepted. A holding recorded as worth zero rather
than omitted. Each is a defect with no tolerable rate, so each is reported as
broken or not and the command exits non-zero when any is.

Two cover the chat agent's write path, which is the only place a model's reading
of a sentence reaches the ledger. A proposal carrying a result must have been
confirmed — a result on a cancelled or expired one means something applied a
change the owner declined or never answered. And every cash flow or trade marked
as coming from chat must have a confirmed proposal behind it, or a write reached
the book around the buttons. A trade especially: it is wrong in every derived
number until somebody notices. Neither says whether an answer was any good; that
has no threshold worth inventing.

Possible percentage forecasts are an advisory observation listing matching
recommendations, refusals, assessments and chat answers across all history. The regex can
match negations or legitimate sourced statistics and miss other phrasings.
Inspect the named rows; a match alone does not fail `eval`, and no match is not
proof that model text complies with the probability rule.

**Observations** are numbers with no correct value: how many claims rest on a
source, how many holdings are held on a thin reason, how often the rules
refused a proposal, truncation and fallback rates, spend, coverage. For chat:
questions asked, model calls per question, spend, and how proposed writes ended.
Two calls a question is the ordinary shape — one to pick a tool, one to answer —
so a rising figure means the loop is not converging. An expired proposal more
often means the sentence did not make sense than that the owner changed their
mind.

A note the agent appended that looks like it restates a figure the ledger holds
— a weight, a value — is listed for review. Those are wrong within the week,
because the note is replayed into later research while the ledger moves on. The
owner's own numbers are legitimate, so this reads the line to you rather than
failing. Two of them
are split by prompt version — the sourced share per analyst prompt, and trades
and REVIEWs per published decision batch per decide prompt, counting proposals
the rules refused and completed batches with no proposals. Both prompt splits
use the run date and the `--weeks` window. Attempts without a published batch
are counted separately, including failures, invalid output and work still in
progress; they do not represent deliberate inactivity. Model-call and cost
metrics also use `--weeks`; other observations describe current state or stored
history. These descriptive counts alone cannot attribute a change to a prompt
or establish improvement. Process quality can be evaluated on controlled cases;
investment skill is not established by these observations.

What `eval` deliberately does not measure is whether the advice was any good.
Fourteen holdings a week will never produce the sample size for that, and
replaying a past week cannot validate a model whose training already contains
what happened next — a favourable historical result would measure leakage
rather than judgement.

## The benchmark

`benchmark` compares the portfolio against the same money left in a global
tracker. This is the only measure of the system likely ever to mean much: at
fourteen holdings a week nothing else has the sample size to separate skill
from noise.

The comparison is **money-weighted**, not a return against a return. Money
arrives over time, so "the portfolio is up 3% and the index is up 5%" answers a
question nobody asked — it assumes every euro was present from the start.
Instead each euro that entered the account buys tracker units at that day's
close, building a shadow portfolio moved on exactly the same dates. Money that
arrived in February cannot capture January's rise, in either column.

Cash flows internal to the account — dividends, fees — do not move the shadow,
because no new money arrived. A contribution on a day the market was shut uses
the previous close, which is what an investor could actually have done;
requiring an exact match would silently drop those contributions instead.

The comparison says when it does not yet mean anything, and stops saying it on
its own once enough time has passed. Recording begins in week one because the
series cannot be reconstructed later; reading it as a verdict should not, and a
number shown without that caveat invites exactly the mistake the system exists
to avoid.

## The weekly report

`report` renders review health, valuation coverage, pending actions, thesis
proposals, blocked trade proposals and approved actions awaiting execution.
`--telegram` sends action-specific buttons: acknowledge a review question,
approve a trade, reject, snooze, view evidence or review the thesis.

A completed review with no pending actions is different from no review or a
failed cycle. Stage outcomes persist, so `/review` continues to show failures.
Missing holding prices suppress aggregate return; stale prices and FX are named.
The first report after an upgrade has no verified cycle history until `weekly`
runs. Opening an existing database applies the workflow migration automatically.

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

## Query views

`db schema` prints five views alongside the tables. They exist so SQL against
the ledger does not have to rebuild the ledger's arithmetic:

| View | Holds |
| --- | --- |
| `v_trades` | trades with the ticker, name, currency and sector spelled out |
| `v_cash_flows` | cash flows, unchanged |
| `v_cash_ledger` | every EUR movement in one signed column: a buy costs its fee, a sell returns net of it |
| `v_cash_balance` | one balance per account, absent for an account that has never moved money |
| `v_latest_price` | the newest stored close per security, in native currency |

`cash_eur` reads `v_cash_balance` rather than recomputing the same sum in
Python, so there is one definition of cash and not two.

Position cost basis is deliberately **not** a view. Average cost releases basis
in proportion to the units held at that trade, which is path-dependent; it stays
in `portfolio.positions`. `v_latest_price` is likewise not a valuation — it is
native currency and says nothing about an unpriced security, because converting
a price and reporting a missing one as `None` is what `holdings` is for.

## Chat

Anything sent to the bot that does not start with `/` is a question, answered by
a model with tools. Ask in plain words — "what did I pay for BRK.B", "how much
cash", "when did I last add money", "which holdings have I never sold any of".

`/reset` forgets the conversation. The last twenty messages are kept in memory
for follow-up questions and are lost when the daemon restarts; the portfolio
itself is read from tools every turn, never remembered, because it changes and
a buffer does not.

A question is answered on the profile's own worker thread rather than on the
polling loop, so a turn taking several seconds does not stall button presses
behind it. Two questions may wait; a third is refused with a message rather than
queued, because queueing only makes the asker's own reply later. The bot sends
"Working…" immediately and rewrites that message with the answer.

The model may call tools up to `CHAT_MAX_TOOL_ITERATIONS` times in one turn,
then must answer with what it has. It defaults to the flash tier like every
other stage — `main.py models` shows the route and `main.py llm-log` the cost.
A turn is two calls and a fraction of a cent.

### Charts

Ask to see something — "chart my weights", "show me the cash over time" — and the
bot sends an image before the text. The model writes plotly code in a `<chart>`
block against the rows it already fetched; the block is removed from what you
read.

The code runs in a **separate process** with no filesystem, network or
subprocess access, and imports allowlisted to plotly, numpy, math and datetime.
A process rather than a thread because a thread cannot be killed: a runaway loop
abandoned on one goes on burning a core and starves every later chart for the
life of the daemon.

A chart that fails falls back to a plain bar or line drawn from the same rows,
then to nothing. The answer's text is written to stand without the picture, so
losing it costs detail and not meaning.

`CHART_EXEC_TIMEOUT_S`, `CHART_WIDTH_PX` and `CHART_HEIGHT_PX` are the knobs.

### Proposed writes

Chat can also record two things, and records neither by itself. Say "topped up
to €250", "got €1.20 from BRK.B", "took out €50", or "add to my log: bought more
BRK.B because the float argument still holds", and the bot replies with one
sentence and two buttons. Nothing changes until the tap.

| Proposal | Becomes |
| --- | --- |
| Cash moved | a `cash_flows` row: contribution, withdrawal, dividend or fee |
| A trade | a `trades` row, in a security already held |
| A note | one dated line appended to `log.md` or `watchlist.md` |

The sentence always states the **resulting** state — "cash goes €90.00 →
€250.00" — because "topped up to 250" and "topped up 250" sound alike and differ
by whatever the balance already was. A misreading is then visible before it is
recorded rather than afterwards.

Proposals are validated when they are made, not when they are applied: a lower
target balance cannot be a contribution, a future date is refused, and a
movement of nothing is refused. What gets applied is the stored proposal, so the
sentence agreed to and the write that follows are the same thing. Confirming
twice writes once.

A trade is the most consequential of these, because every derived figure —
position, cost basis, weights, cash — comes from the ledger. Its summary states
the position and the cash on both sides: *"Position goes 4 → 6 units; cash goes
€50.00 → €20.00."* A sell of more than is held is refused rather than capped;
`positions()` caps an over-sell silently, and a proposal must not lean on that.
A buy beyond the available cash is **allowed** and flagged — the broker executed
it, so a negative balance means a deposit is missing from the book, not that the
trade is wrong.

Say a price per share and it is converted at the latest stored rate, with that
rate shown before you agree: "bought 2 at 470" on a US listing is 470 dollars,
and a trade recorded at the wrong FX is a small error that never corrects
itself. With no rate stored the proposal is refused and points at `sync`.

**Only a security already held.** Something new needs its listing currency and
feed symbol set deliberately, or nothing can ever price it; there is no path for
that from chat, and the agent says so instead of guessing. Correcting an
existing trade is likewise not a chat operation — a wrong correction rewrites
history and everything derived from it.

A proposal expires after `PROPOSAL_TTL_MINUTES`, because it is arithmetic
against the balance at the time it was made; the daemon closes unanswered ones
on its usual sweep. Ask again rather than confirming something stale — the
figures are re-read.

`strategy.md` and `investor.md` cannot be appended to from chat. They constrain
every recommendation the weekly run makes, so changing one is an edit to read in
full, not a line added from a phone. `history.md` is generated.

Notes are held to one line that stands on its own, because they are replayed
into later research prompts without the conversation that produced them. A note
must not restate a value, weight or return: the ledger holds those and they will
have moved by the time the note is read.

**Chat answers what is and what happened. It does not say what to buy or
sell** — that comes from the weekly run, which has guardrails, sourced
evidence and thesis review. Asked anyway, it says so and points at `recommend`.
It states no probabilities, confidences or price targets, and does not judge the
portfolio diversified or risky: it reports weights against the configured limits
and leaves the judgement to the owner.

## Chat tools

The chat agent reaches the database through three tools, and the split between
them is deliberate.

| Tool | For |
| --- | --- |
| `run_sql` | Read-only SQL: history, filtering, counting. Trades, cash flows, prices, recommendations, theses, research runs |
| `portfolio_snapshot` | The authoritative current state: positions, cost basis, value, return, cash, total |
| `concentration_report` | Weights by security, sector or theme, with the limits they are judged against |
| `propose_cash_flow` | Asks to record money that moved. Writes nothing |
| `propose_trade` | Asks to record a buy or sell in a held security. Writes nothing |
| `propose_context_note` | Asks to append one line to `log.md` or `watchlist.md`. Writes nothing |

`run_sql` opens the database `mode=ro`, so it cannot write and cannot create
one. It accepts `SELECT` and `WITH` only, caps rows, truncates an oversized
cell, and abandons a query that runs too long — the knobs are
`CASH_ASH_CHAT_SQL_ROW_LIMIT` and `CASH_ASH_CHAT_SQL_TIMEOUT_S`. The schema it
writes against is read from the live database on every turn rather than
described in the prompt, so a migration cannot leave the two disagreeing.

Results come back as markdown tables and prose, not JSON. Repeating a key on
every row costs tokens, and `"value_eur": null` leaves the reader to work out
what it means where "UNPRICED — excluded from the total" says it.

The two `propose_*` tools return a validated proposal rather than applying it,
which is what keeps every tool here read-only. The daemon persists it to
`pending_write` and puts the buttons on it, and only the button writes.

**SQL is not allowed to answer a money question.** Cost basis is path-dependent
and an unpriced holding reports nothing rather than zero, so a hand-written
`SUM(quantity * price)` would produce a plausible wrong figure. Every euro
amount, weight and return comes from `portfolio_snapshot` or
`concentration_report`, which return what the rest of CashAsh computes. Both say
which holdings they could not price and that the total excludes them.

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

Every update must pass three tests before anything happens: the sender's
numeric Telegram id is in the roster and enabled, the chat is **private**, and
the chat id equals the sender id. **An update failing any of them is ignored in
silence** — not answered, not acknowledged. The bot's username is discoverable,
and replying would confirm the bot is live and tell a stranger their id is
merely not on the list. A disabled profile is treated the same way.

The private-chat test is what stops a holding being read out into a group: the
holder of a roster id can be in one with people who are not. In a private
one-to-one chat the chat id and the sender id are the same number, so an update
where they differ is not the conversation it claims to be. A roster
`telegram_id` must be positive, because an update with no sender at all — a
channel post, an anonymous admin — reads as id 0.

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

Every finding is `sourced`, `background` or `unanswered`. An unanswered question
stays unanswered in storage. A sourced answer must reference a supplied evidence
ID and copy an exact supporting excerpt. URL and date come from that item, not
from model output. Unknown IDs and invented excerpts become unanswered findings.
Membership and quote checks do not prove semantic support; inspect the source.

Coverage is sufficient only when every planned question has a sourced answer
that passed citation validation. Background explanations remain stored, but
cannot substitute for evidence about current company facts. Extra answers the
analyst volunteers beyond the plan count neither for nor against coverage. On
database migration, historical labels are recomputed under this rule; findings,
evidence packages and owner theses are preserved. Even sufficient coverage does
not establish that the questions addressed valuation or that an investment is
attractive.

In practice the default news feed rarely sources every planned question, so
BUY, ADD and EXIT stay refused until you supply targeted excerpts. `research
TICKER` ends by listing the planned questions still without a sourced answer,
verbatim, so they can be pasted into the `questions` field of an entry in
`context/evidence.json`; the Telegram `/evidence TICKER` view shows the same
list. `main.py process` reports the unanswered count across holdings.
Each rerun generates a new plan. Question matching uses text containment, so a
rephrased question can exclude a previously tagged excerpt. For relevant
material you want available across plans, set its `questions` field to `[]` to
match by symbol instead; the analyst still has to judge whether it answers the
new questions. Reusing a previous plan is not currently supported.

Evidence packages are stored with a content hash alongside completed assessments,
including unchanged assessments and open questions. These dated assessments feed
the decision stage separately from the active thesis. A proposed thesis is never
adopted automatically.

News aggregation is the default. For question-specific primary material, put a
JSON list in `context/evidence.json` under your profile, or pass
`main.py research TEST --evidence-file /path/to/evidence.json`. Local excerpts
matching the symbol and question are considered before news. `questions: []`
applies an excerpt to any question for that symbol. Use the provider symbol.

```json
[
  {
    "symbol": "TEST",
    "questions": ["What happened to operating margin?"],
    "title": "Synthetic quarterly release",
    "url": "https://example.com/investors/quarterly-release",
    "published": "2026-09-01",
    "publisher": "Synthetic issuer",
    "excerpt": "Operating margin was 12% for the quarter."
  }
]
```

These are supplied excerpts; the app does not automatically fetch filings or
verify their transcription. Keep personal research under `CASH_ASH_HOME`.
`EVIDENCE_ITEMS_PER_SECURITY` limits package size and `EVIDENCE_MAX_AGE_DAYS`
excludes old material. `main.py process` prints both. Future-dated and malformed
items are excluded. Unsupported instruments need manual review rather than an
assumption that a company analyst can handle them.

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

`init` reads a broker snapshot from `$CASH_ASH_HOME/seed_snapshot.toml`. That
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


## Thesis review and execution

```bash
uv run python main.py thesis show TEST
uv run python main.py thesis accept TEST 2
uv run python main.py thesis reject TEST 3
uv run python main.py cash 25 --date 2026-09-07 --note "Actual deposit"
uv run python main.py decide 12 approve
uv run python main.py executed 12 --quantity 0.5 --amount 20 --fee 0.10 --date 2026-09-07
uv run python main.py executed 13 --not-executed --note "Changed my mind"
uv run python main.py process
```

Figures and IDs above are illustrative. `thesis show` labels the active version
and pending revisions separately. Acceptance refuses a proposal based on a
superseded thesis. Telegram equivalents are `/thesis TEST`, `/accept TEST 2`
and `/reject TEST 3`; `/evidence TEST` opens the latest findings and excerpts.

`cash` records an actual positive EUR contribution, never an expected deposit.
`decide ID approve` rechecks trade constraints. A repeated identical decision is
idempotent. Reject an approved action to release its reserved cash. An approval
remains reserved after expiry until explicitly resolved.

`decide ID later` snoozes until `SNOOZE_DAYS`, bounded by the recommendation's
expiry. It returns in `/pending` when due; the daemon sends a reminder on its next
scheduler check. Expired or replaced recommendations are not reminded.

`executed` records one actual fill per approved trade recommendation and links
it to the ledger atomically. Amount is EUR consideration excluding the fee.
Oversells, duplicate execution, nonfinite values and invalid dates are refused.
A recorded actual fill may differ from the proposal; it describes what happened.
`--not-executed` closes an approved action without adding a trade. Partial fills
across multiple executions and broker imports are not supported.

`process` prints observed coverage, unanswered questions, verified/rejected
citations, decision reversals, recorded model cost and effective research limits.
It measures process, not investment skill. Unknown model costs remain unknown.
