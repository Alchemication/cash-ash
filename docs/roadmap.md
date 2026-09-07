# Roadmap

What is not built yet, in the order it should be, and the reasoning behind the
order. Unlike the rest of `docs/`, this file is explicitly about the future.

## Where the design came from

The original spec proposed the whole pipeline at once: research planner,
bull/bear/independent analysts, critique and synthesis, portfolio rules,
Telegram approval, evals. The critique that shaped these phases:

**Scale.** ~EUR 1,400 across 14 positions. A weekly planner + 3 analysts +
critic + synthesiser is ~84 model calls a week before evidence gathering. Model
spend can plausibly exceed any excess return the system could earn. Accepted —
the goal is research habits and an interesting system — but it forces cheap
default routing and opt-in escalation.

**BUY and ADD were unreachable.** EUR 4.15 cash. Without an expected
contribution the decision layer can only emit HOLD/TRIM/EXIT and half the
system is dead code. Hence `cash_flows` and a planning contribution constant.

**Currency was missing entirely.** USD-listed holdings, EUR quotes, and a
broker return percentage that blends stock performance with EUR/USD. Left
unmodelled, this silently corrupts every unrealised gain the decision layer
reads. Hence EUR book + native prices + separate `fx_rates`.

**Holdings are not uniform, even when they all have tickers.** SPCX was seeded
as an unpriceable pre-IPO wrapper; it had in fact listed on Nasdaq on
2026-06-12, three months before the snapshot. The correction is instructive:
the special case was real, just not the one assumed. A stock one quarter into
public reporting has no long-run comparables, thin analyst coverage and a
lockup expiry, which is a research problem rather than a pricing one.
`pricing_mode` and `asset_class` stay because the pipeline must be able to skip
a security rather than assume uniformity — but nothing currently exercises
them, so treat both as untested until something does.

**Bull/bear roles are the weakest idea in the spec.** Assigning a model to argue
a side manufactures motivated reasoning, and a real bear signal becomes
indistinguishable from an obedient one. Phase 5 uses neutral analysts on
identical evidence and treats *disagreement between them* as the signal. Keep
adversarial roles as a config flag to test later, not as the architecture.

**Outcome evals cannot work at this scale.** 14 tickers a week will never
separate one model's stock picking from another's, and historical replay leaks
the future through the model's own weights. Evals measure process; outcomes and
the passive benchmark are recorded, never claimed as evidence.

**Concentration needed no AI at all** and was the most useful thing available on
day one — which is why it shipped in Phase 1.

## Phases

| Phase | Content | Status |
|---|---|---|
| 0 | Scaffolding: `uv`, `src/` layout, dispatch-only `main.py`, config, logging, migrations, lint + tests | done |
| 1 | Ledger: accounts, securities, trades, cash flows, snapshots. Seed, `holdings`, `concentration` | done |
| 1.5 | Profiles: roster, per-person directories, `--profile` everywhere, context-file scaffolding, `doctor` | done |
| 2 | `MarketDataProvider` abstraction, yfinance adapter, price + FX fetch, `sync`, manual-price entry for when a feed fails | done |
| 2.5 | Events and consensus estimates: known dates from the feed, curated dates from a file, an estimate time series | done |
| 3 | LLM plumbing: retry, fallback, per-feature routing, call/trace logging, `models`, `llm-log` | done |
| 4 | Versioned theses, research runs, evidence. Plan → evidence → single analyst → structured thesis update. `research TICKER` | |
| 5 | Frozen hashed evidence package, N neutral analysts across providers, disagreement scoring, synthesis | |
| 6 | Decision layer: deterministic guardrails, recommendations with expiry, decisions | done |
| 7a | Telegram: notifier, weekly report, Approve/Reject/Later keyboards | done |
| 7b | Long-polling daemon for button callbacks, launchd scheduling | next |
| 8 | Process evals, curated cases, passive-benchmark tracking | |

## Why profiles landed before market data

Retrofitting `--profile` after market data, model routing, research and
Telegram exist means touching every command, every database open and every
test. zdrowskit carries a `profile adopt` command purely to migrate legacy
single-profile installs; that command is the retrofit tax, and building the
roster early avoids paying it.

One bot serves every profile, routed by the numeric `telegram_id` in the
roster. Per-person bots would mean a BotFather registration, a token and a
polling loop each, for nothing.

Context files are created and managed now but read by nothing. That is
deliberate: `strategy.md` is the file that gets filled in over weeks, and it
should already exist when the research pipeline arrives. An unedited template
counts as unwritten, because placeholder prose consumed as intent is worse than
an absent file.

**Open fork, to decide at Phase 4.** Research is per-company; portfolios and
decisions are per-person. Three profiles all holding AAPL would run the analyst
panel three times for identical output, tripling the cost already flagged
above. The split when it matters: securities, prices, FX, evidence and research
runs shared; trades, positions, theses, recommendations and decisions
per-profile. Not built now — there is one profile and no research — but the
research tables stay free of profile-scoped ids so it remains available.

## Notes from Phase 2

Yahoo publishes both directions of a currency pair, so `USDEUR=X` is requested
directly rather than inverting `EURUSD=X`. Inverting is where direction errors
live, and an FX bug misprices every holding at once rather than one.

The provider reports a number, not a currency. `securities.currency` stays the
authority on what that number is denominated in, so a provider quirk cannot
silently redenominate a holding.

`pricing_mode` stopped being a dead column: `sync` skips manual securities
without reporting them as failures, and `price` enters one by hand. Nothing in
the seeded portfolio uses it today, but a delisting, a halt or an outage turns
it on with no warning.

Prices are stored under the close's own date, never the request's. A Monday
sync returns Friday's close, and stamping it Monday would make every price look
fresher than it is — which is exactly what the staleness check exists to catch.

## The triage design

The weekly run is a funnel, not a fixed grid. Every holding is analysed each
week — the owner is learning the market, and a stock where nothing happened is
still a worked example — but not all at the same depth, and almost none of it
is notified.

```
Tier 0   deterministic, every holding, no model
         price moves, weight drift, concentration, staleness, guardrails

Tier 1   triage: ONE call covering every holding together
         "given these moves, dates and open questions, what deserves depth?"
         A comparative judgement, so it is both cheaper and better done once
         than fourteen times.

Tier 2   depth where warranted: earnings, material move, thesis flag, or the
         six-week staleness floor. The multi-model panel fires here only.

Tier 3   one portfolio decision across everything.
```

The staleness floor is the important part. Pure change-detection has a fatal
gap: a company that deteriorates slowly never trips a trigger. Nike did not
fall 37% in a week.

**Notification is not gated on model-reported confidence.** An LLM's stated
certainty is uncalibrated, and filtering on it rewards overconfidence — the way
to be heard becomes claiming to be sure. Gates are things that can be checked:
a thesis changing state, a deterministic guardrail, independent agreement
between models from different labs reading the same frozen evidence, and
whether claims carry sources.

Cost is not what drives any of this. At the default routing a full weekly run
over fourteen holdings is a few dollars a year. The constraint is that output
nobody reads is worse than no output.

## Notes from Phase 2.5

Earnings, ex-dividend and dividend dates come from the feed. Everything else
does not exist in any API — a keynote, an IPO lockup expiry, a delivery report
published ahead of earnings — and those are frequently the dates that move a
holding, so they are curated in a file the user maintains.

Analyst consensus is recorded as a series keyed by observation date. The
provider reports today's number and never last month's, so an estimate revision
is only visible against something already stored. Recording began before
anything consumed it because this series cannot be backfilled.

Two bugs surfaced while testing the storage, both worth recording. `INSERT OR
IGNORE` swallows every constraint failure, not just uniqueness, so invalid data
vanished instead of raising. And SQLite treats two NULLs as distinct in a
unique index, so an event belonging to no single holding never collided with
itself and gained a duplicate on every weekly sync.

## Notes from Phase 3

Routing is per stage and defaults to the cheap tier everywhere. The cost
argument is only real if it is the default rather than the advice.

Two behaviours came from measurement, not design. The default model always
reasons and cannot be told not to: asked a one-sentence question with a
400-token budget it spent all 400 thinking and returned empty content with
finish_reason 'length'. So output budgets have an enforced floor callers cannot
lower, and a reply truncated before any content appears is retried once with a
larger budget.

The fallback behaviour on truncation was got wrong twice before the live
evidence settled it. Blocking fallback looked right — a prompt asking for more
than fits is a prompt problem, and another model hits the same wall. But a live
run showed glm-4.7 answering a prompt glm-5.3-flash could not finish, because
reasoning verbosity differs between models. Allowing the fallback its own
escalation was then also wrong: it compounded to four attempts and 12,500
tokens. What ships is one enlargement in total, carried across models: primary,
primary enlarged, fallback at that same enlarged size, then stop.

Every attempt is logged including failures, because a model that fails
repeatedly is what an evaluation needs to see and it is invisible if only
successes are kept. Traces group the calls of one operation, since a synthesis
read alone says little about why it concluded what it did.

## Deliberately out of scope for v1

- **Automatic trading.** Execution stays manual behind a `BrokerAdapter` seam.
- **New-stock discovery.** The spec's screening workflow is the most expensive
  part and the least useful while there is no capital to fund a new position.
- **Multi-account tracking.** `accounts` carries one row per profile so a
  second broker is an INSERT rather than a rewrite, but no features are built
  on it. Distinct from profiles, which are multiple *people*, each with one
  broker.
- **Tax lots and vesting.** Average cost only. Revisit if an account with
  restricted stock is ever brought into advice scope.
- **Crypto research.** The pipeline is fundamentals-shaped — thesis, valuation,
  earnings, estimates — and none of that has a crypto analogue. A panel with no
  evidence base produces confident narrative, which is the exact failure the
  evidence-provenance rule exists to prevent.
