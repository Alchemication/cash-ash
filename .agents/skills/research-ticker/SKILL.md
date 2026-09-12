---
name: research-ticker
description: Walk one ticker through the CashAsh research pipeline stage by stage, stopping after each so the owner can see what went in, what came out, and which step a model touched. Read-only by default — it reconstructs a stored run rather than producing a new one. Use when researching or reviewing a single stock, when tracing why the pipeline reached a verdict or refused an action on one ticker, or when asked to explain how the research machinery works. Covers a current holding and a ticker not in the portfolio.
---

# Research one ticker, one stage at a time

Everything the pipeline does is already recorded: the triage ranking and its
reason, the planned questions, the frozen evidence package, every validated and
rejected citation, the assessment, any proposed thesis, the recommendation with
its guardrail arithmetic, and the full prompt, reasoning, response and cost of
every model call. So the default mode reconstructs a run rather than producing
one. No model calls, no cost, nothing durable written, and it can be run twice
with the same result.

Producing new findings is a second mode, entered only when the owner asks for
it in as many words.

## Rules

- Every command is `uv run python main.py ...` from the repo root. Never plain `python`.
- **Default to inspect mode.** Run nothing that calls a model or writes a row
  until the owner explicitly asks for a live pass.
- **Never run `main.py weekly`, `decide`, `executed`, `cash`, or
  `thesis accept|reject|bootstrap`.** Not with permission, not on request —
  point at the command and let the owner run it.
- **Never run `research` or `recommend` without `--no-store`.** Those are the
  owner's to run when they want a result kept. With `--no-store` the same code
  runs against a discarded copy of the database, so a stored run is always one
  the owner chose.
- **Stop after every stage.** Report, then wait. Do not chain stages.
- Every figure you state comes from output you just ran. Never restate a number
  from memory, never compute one yourself, never round for neatness.
- Do not research the ticker yourself — no web search, no recalled facts, no
  running the prompts in `src/prompts/` by hand. The point is to observe this
  pipeline; a second unlogged one beside it destroys that.
- Model output is not authority. A `thesis_status` is one model's read of a
  handful of news blurbs. Never restate a model's confidence as a probability.
- Read `references/pipeline.md` before explaining why a stage did what it did,
  and `references/inspect.md` for the queries.

### What "read-only" means exactly

Inspect mode issues `SELECT`s through `sqlite3 -readonly` and the read commands
below. Two caveats to state honestly rather than hide:

- Every CLI command opens the database with migrations enabled and sets WAL
  mode, so even `holdings` touches the file. With no migration pending that
  changes no data.
- `llm-log` is read-only and free. Use it freely.
- A `--no-store` run is not inspect mode. It calls models, costs money, and
  writes their log rows — deliberately, since the spend has to be accounted
  for somewhere. Nothing else it produces is kept.

## Step 0 — route

```bash
uv run python main.py holdings
uv run python main.py doctor        # note the database path for the queries
```

Ticker in the table → continue below.
Ticker absent → read `references/candidate.md` and follow it. Do not improvise
a path through the held route.

## Inspect mode — the default

Six stages. Stop after each. `references/inspect.md` has the exact query for
every one; run them read-only against the path `doctor` printed.

### 1 — Standing

```bash
uv run python main.py holdings
uv run python main.py process
```

What the holding is worth, its weight, whether prices are stale (`holdings`
prints its own warning with the threshold), and when it was last researched.
`process` prints the live limits — quote those, never a constant read out of
`src/config.py`.

### 2 — The baseline

```bash
uv run python main.py thesis show TICKER
```

The owner's own stated reason, which is what every later stage is measured
against. Read out the summary, conviction, assumptions and especially `what
would break it` — the analyst may only report a condition from that list.

Note any version sitting at `proposed`: a past run wanted to change this and
the owner has not ruled on it.

### 3 — Why it was picked up

Query the last few `triage_result` rows for the ticker: rank, selected, the
reason in the model's words, the signals. If it was never selected, say so —
a holding can go `RESEARCH_OVERDUE_DAYS` without a look, and the rotation slot
rather than any signal is often what eventually pulls it in.

### 4 — What the pass actually did

Query the latest `research_assessment` for the ticker. It holds everything:

1. **Planned questions** — written from the thesis alone, before any evidence
   was fetched, and retrieval is not question-aware. This is why coverage so
   often comes back short.
2. **The frozen evidence package** — the exact items the analyst saw, with a
   content hash. Read it out; it is usually the most revealing artefact.
3. **Answers**, each tagged `sourced`, `background` or `unanswered`, some
   carrying a `validation_error` where a citation was rejected and the answer
   rewritten.
4. **Coverage**, and which questions have no validated sourced answer.
5. **Status and reason**, plus anything reported as a triggered breaking
   condition.

Say plainly what `sourced` means: the quoted words appear verbatim in the
headline and blurb that were passed in. It proves the analyst did not invent
the string. It does not prove the claim is true or that it supports the thesis.
Show the excerpt beside the claim and let the owner judge.

`references/pipeline.md` lists the deterministic rewrites that happen after the
model answers — the forced `unchanged`, the demoted `broken`, the appended
unanswered questions. When output looks odd, check there before blaming the
model.

### 5 — The recommendation, and why it is what it is

Query `recommendation` for the ticker and `decision_refusal` for the run. Report
separately:

- **what the model proposed** — rationale, urgency
- **what the rules did to it** — refusals and adjustments verbatim

`recommendation.guardrails` holds the arithmetic behind each verdict, stored as
JSON: the capital left, the weight against its cap, what a limit clamped. Read
it out rather than reasoning about what the rules probably did.

A refusal is the system working. Name which rule fired; `references/pipeline.md`
lists them in firing order.

### 6 — The model calls themselves

```bash
uv run python main.py llm-log --trace N     # every call in one operation
uv run python main.py llm-log --id N        # prompts, reasoning, response, cost
uv run python main.py models                # the route each stage takes
```

The full prompt is stored. When the owner asks why a stage concluded something,
this is the answer — read what it was given, not what it should have been.

Close by stating what the whole exercise cost: nothing.

## Live mode — watching a stage run, on request

Two commands qualify, both only with `--no-store`:

```bash
uv run python main.py research TICKER --no-store
uv run python main.py recommend --no-store
```

They run exactly what the weekly cycle runs — same prompts, same models, same
billing — against a copy of the database that is thrown away at the end. The
model-call log is the only thing kept, so `llm-log` still accounts for the
money. `recommend --no-store` supersedes nothing, so a pending recommendation
from the weekly run is safe.

Before running either, say and wait:

- how many calls it makes — research is two, planner then analyst; recommend is one
- that the cost is real, and read it out afterwards from the command's own
  closing line
- what will *not* exist afterwards: no run, no evidence, no assessment, no
  proposed thesis, no recommendation to decide on

Afterwards, walk inspect stages 4 to 6 over what it printed, and stage 6's
`llm-log` for the calls it just made.

Insufficient coverage is the normal outcome and it is what blocks BUY, ADD and
EXIT. Rerunning changes nothing on its own — follow `references/evidence-loop.md`,
which needs the owner to add excerpts.

### Comparing models

`--model FEATURE=MODEL` routes one `--no-store` run differently — several
features comma-separated. Features are the stages `main.py models` lists.

```bash
uv run python main.py research TICKER --no-store --model analyst=MODEL
```

Use it to ask what a stronger model would have concluded on the same thesis and
the same evidence, then compare in `llm-log`. Two honest caveats to state every
time: the evidence package is re-fetched, so a difference is not necessarily the
model; and one ticker on one week is an anecdote, not a comparison. Defaults are
cheap for a reason — say what the run cost.

It is refused without `--no-store`, so a stored result always matches the
routing table.

## What still requires the owner

`decide`, `executed`, `thesis accept|reject`, and any run whose result is meant
to be kept. Explain, point at the command, stop. Recording a belief or a
decision on their behalf is the one thing this pipeline exists to prevent.
