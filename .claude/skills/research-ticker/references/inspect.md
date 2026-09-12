# Read-only queries

Every query here is a `SELECT`. Resolve the database path first, then open it
read-only — never open it for writing, and never let a look apply a migration:

```bash
DB="$(uv run python -c "import sys; sys.path.insert(0,'src')
from profiles import resolve_cli_profile
print(resolve_cli_profile(None, db=None)[1])")"
sqlite3 -readonly "$DB" "SELECT ...;"
```

That reads the roster and prints a path; it opens nothing. `main.py doctor`
shows the same path, but rich wraps long values in a narrow terminal, so do not
try to parse it. Add `--profile NAME` handling by passing the name as the first
argument to `resolve_cli_profile`.

Add `-header -column` for a readable table, or `-json` when you need to pull a
JSON column apart. Long JSON columns are easier read one at a time than in a
row of many.

## Runs that touched this ticker

```sql
SELECT r.id, r.run_date, r.kind, r.trace_id, r.note
FROM research_run r
JOIN research_assessment a ON a.run_id = r.id
JOIN securities s ON s.id = a.security_id
WHERE s.ticker = 'TICK'
ORDER BY r.id DESC LIMIT 5;
```

`trace_id` is what `llm-log --trace` takes.

## Triage: why it was or was not picked up

```sql
SELECT r.run_date, t.rank, t.selected, t.reason, t.signals
FROM triage_result t
JOIN research_run r ON r.id = t.run_id
JOIN securities s ON s.id = t.security_id
WHERE s.ticker = 'TICK'
ORDER BY t.run_id DESC LIMIT 5;
```

`signals` containing `omitted` means triage never considered it — the model
left it out of its reply and it was recorded as unranked rather than dropped.

## The assessment — the centre of a run

```sql
SELECT status, coverage, reason, triggered_json, open_questions_json, package_hash
FROM research_assessment WHERE run_id = N;
```

Then each JSON column on its own:

```sql
SELECT questions_json FROM research_assessment WHERE run_id = N;   -- what was planned
SELECT answers_json   FROM research_assessment WHERE run_id = N;   -- kind, quote, validation_error
SELECT package_json   FROM research_assessment WHERE run_id = N;   -- the frozen evidence, verbatim
```

`package_json` is the exact evidence the analyst saw, and `package_hash` pins
it. An answer carrying `validation_error` is one whose citation failed the
verbatim check and was rewritten to `unanswered`.

## Evidence rows as stored

```sql
SELECT e.kind, e.source_title, e.claim, e.source_url, e.published_date
FROM evidence e JOIN securities s ON s.id = e.security_id
WHERE s.ticker = 'TICK' AND e.research_run_id = N;
```

`source_title` holds the question, `claim` the answer.

## Theses, including proposals nobody has ruled on

```sql
SELECT t.version, t.status, t.thesis_status, t.research_run_id, t.llm_call_id,
       t.created_at, t.summary
FROM thesis t JOIN securities s ON s.id = t.security_id
WHERE s.ticker = 'TICK' ORDER BY t.version DESC;
```

`status` is `active`, `superseded`, `proposed` or `rejected`. A `proposed` row
is a suggestion waiting on the owner, never in force.

## Recommendations and the arithmetic behind them

```sql
SELECT r.id, r.run_date, r.action, r.amount_eur, r.urgency, r.weight_pct,
       r.price_native, r.fx_rate, r.adjusted, r.llm_call_id, r.expires_on,
       r.superseded_by_run_id, r.rationale
FROM recommendation r LEFT JOIN securities s ON s.id = r.security_id
WHERE s.ticker = 'TICK' ORDER BY r.id DESC LIMIT 5;

SELECT guardrails FROM recommendation WHERE id = N;
```

`guardrails` is JSON holding `checks` (the limits evaluated, with the numbers
that stood at the time) and `adjustments` (what clamped the amount, in words).
`price_native` and `fx_rate` are frozen at the moment of the recommendation —
that is the forward-tracking record, not a current quote.

## Refusals

```sql
SELECT run_id, action, ticker, rationale, refusal
FROM decision_refusal WHERE ticker = 'TICK' ORDER BY id DESC LIMIT 10;
```

`rationale` is what the model argued; `refusal` is why the rules said no.
Refusals are kept precisely so this comparison is possible.

## The week's summary as the decision stage wrote it

```sql
SELECT run_id, summary FROM decision_batch ORDER BY run_id DESC LIMIT 3;
```

## Model calls

Prefer the CLI — it renders prompts, reasoning and response properly:

```bash
uv run python main.py llm-log --trace N
uv run python main.py llm-log --id N
uv run python main.py llm-log --feature analyst --limit 10
uv run python main.py llm-log --errors
```

For a cost total across a run:

```sql
SELECT feature, model, attempt, input_tokens, output_tokens, cost_usd, finish_reason
FROM llm_call WHERE trace_id = N ORDER BY id;
```

A `finish_reason` of `length` means the reply was truncated — findings after
that point were never written, whatever the output implies.
