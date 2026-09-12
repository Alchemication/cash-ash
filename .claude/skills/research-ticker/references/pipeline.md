# What each stage is, sees, and decides

Use this to answer "why did it say that". Verify against the code before
asserting anything — line numbers move.

## The shape of it

A weekly run is `1 + 2N + 1` model calls, N capped at `RESEARCH_MAX_PASSES`.
Everything else is deterministic. All stages run at temperature 0 except the
planner at 0.2, all on the flash tier unless a route was overridden
(`main.py models`).

| Stage | Kind | Sees | Produces |
|---|---|---|---|
| sync | deterministic | held securities | closes, FX, events, estimates |
| snapshot | deterministic | holdings + fresh prices | dated portfolio value |
| triage | **1 model call**, whole portfolio | see below | ranking, selections |
| select_research | deterministic | triage picks, coverage ages | ≤4 targets |
| plan | **1 model call** per ticker | thesis only | ≤6 questions |
| evidence | deterministic | symbol | ≤8 dated items |
| analyst | **1 model call** per ticker | thesis + questions + items | answers, status |
| validation | deterministic | analyst output | coverage verdict |
| decide | **1 model call**, whole portfolio | see below | proposals |
| guardrails | deterministic | portfolio state | refusals, clamps |
| report | deterministic | stored rows | the review |

## triage — `src/research.py`, `run_triage`

One call for the whole portfolio, because the judgement is comparative.

Per holding it is given: weight %, unrealised %, price move (needs two stored
prices, otherwise absent), thesis summary, conviction and status, breaking
conditions, open questions, events in the last 14 and next 21 days, EPS
estimate revision (needs two observations). When a field is unavailable across
the whole portfolio, the prompt says so explicitly — absent and unchanged
render identically otherwise.

Deterministic afterwards: unknown or repeated tickers dropped, malformed ranks
raise, an empty ranking raises, and any holding the model failed to mention is
appended with `signals: ["omitted"]` rather than silently lost.

`main.py triage --dry-run` prints the exact input without calling a model.

## plan — `src/research.py`, `_plan_research`

Sees the date, ticker and name, why triage selected it, and the thesis:
summary, rationale, assumptions, breaking conditions, open questions.

Sees **no evidence and no price**. The questions are written before anything is
retrieved, and retrieval is not question-aware — this is the structural reason
coverage comes back insufficient so often. It is a known gap; `docs/roadmap.md`
calls it query-aware search.

## evidence — `src/research_evidence.py`, `gather_evidence`

Deterministic, no model. Excerpts from the profile's `context/evidence.json`
matched by symbol and loose text overlap with the questions, then generic news
for the symbol from the configured source. Deduplicated by URL, anything
outside 0–`EVIDENCE_MAX_AGE_DAYS` dropped, non-http(s) dropped, capped at
`EVIDENCE_ITEMS_PER_SECURITY`. Curated items take the slots first.

## analyst — `src/research.py`, `research_security`

Sees the date, ticker and name, the thesis with its assumptions and breaking
conditions, the numbered questions, and the evidence items.

Sees **no price, no weight, no cost basis, no cash**. It is assessing a stated
thesis, not valuing a position. "Is this expensive?" is a question this
pipeline structurally cannot answer.

## validation — deterministic, and where most of the work happens

Applied to the analyst's output, in this order:

- a `sourced` answer must name a supplied `E<n>` and its quote must appear
  verbatim in that item's `title\nsummary`; if not, the answer is rewritten to
  `unanswered` with a `validation_error`
- any planned question the analyst skipped is appended as `unanswered`
- a triggered breaking condition that is not in the owner's own list **raises**
  and fails the whole pass
- a status other than `unchanged` with zero sourced answers is forced back to
  `unchanged` and its reason overwritten
- `broken` with nothing triggered is demoted to `deteriorating`
- coverage is `sufficient` only when every planned question has a validated
  sourced answer
- a thesis revision is written as `status='proposed'` at the next version
  number and never adopted

So a green `sourced` means: this string exists in the blurb we passed in.
Nothing more.

## decide — `src/decisions.py`, `run_decision`

Much wider input than research: total value, cash, planned contribution,
reserved and committed capital, tickers awaiting execution, `strategy.md` and
`investor.md` verbatim, the full triage input set again with a `sell_permitted`
flag per holding, stored close and FX and pricing mode for every security as
JSON, and the latest assessment per security including the raw answers and the
owner's review status of any proposed thesis.

The model proposes. `src/guardrails.py` decides.

## guardrails — the order rules fire

1. unknown ticker dropped; `HOLD` dropped as carrying no instruction
2. ticker already awaiting execution → refused
3. valuation gap anywhere in the book → refused
4. `ADD` needs an existing priced holding; `BUY` needs the opposite
5. selling requires a deteriorating or broken thesis, or an oversized position
   — otherwise refused, because price alone is never a reason to sell.
   `EXIT` on a merely deteriorating thesis is demoted to `TRIM`
6. position weight cap
7. single-trade limit, weekly allocation, available funded cash — each clamps
   rather than refuses, in that order
8. `BUY` additionally needs a fresh quote and FX
9. `BUY`, `ADD` and `EXIT` additionally need an assessment with sufficient
   coverage, less recent than `RESEARCH_OVERDUE_DAYS`

Every limit is a named constant in `src/config.py` with a docstring saying how
it was chosen. `main.py process` prints their live values — quote that rather
than the source.

Refusals are stored in `decision_refusal`, not discarded. Surviving
recommendations store the arithmetic in `recommendation.guardrails` and the
model call in `recommendation.llm_call_id`.

## Inspecting a run

Queries for everything below are in `inspect.md`. To watch a stage run instead
of reading one that already ran, `research` and `recommend` take `--no-store`:
the real code against a discarded copy of the database, keeping only the model
log. The CLI for reading what is stored:

```bash
uv run python main.py llm-log                 # recent calls, cost, tokens
uv run python main.py llm-log --trace N       # every call in one operation
uv run python main.py llm-log --id N          # prompts, reasoning, response
uv run python main.py llm-log --errors        # failed attempts
uv run python main.py models                  # route per stage
uv run python main.py process                 # coverage, citation checks, limits
uv run python main.py eval                    # process invariants
```

Anything the CLI does not expose is a read-only query away — see `inspect.md`
for the path resolution and one query per stage. Useful tables:
`research_run`, `research_assessment` (questions, answers, coverage, frozen
evidence package), `evidence`, `thesis`, `recommendation`, `decision_refusal`,
`decision_batch`, `llm_call`, `triage_result`.
