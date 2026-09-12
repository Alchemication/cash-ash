# A ticker that is not in the portfolio

Not supported yet. Say so directly, explain why, and stop. Do not assemble a
substitute out of web search and the prompts in `src/prompts/` — unlogged,
unstored, guardrail-free output that looks identical to a real run is worse
than no answer, in a project whose stated purpose is measuring process.

## What blocks it

- `research_security` refuses a ticker with no row in `securities`, and only
  `main.py init` creates rows.
- It also refuses a security with no active thesis. Research compares evidence
  against a stated reason; there is nothing to compare to.
- `thesis bootstrap` iterates holdings, so it cannot produce that thesis.
- `sync` prices held securities only, so the BUY freshness gate could never
  pass.
- `recommend` discards proposals for tickers it cannot resolve.

Verify each against the code before stating it — this list will go stale.

## What it would take

A candidate concept, small but real: a migration marking a security as watched
rather than held, one command to create it, a thesis written by the owner as a
hypothesis to test rather than restated from notes, and `sync` extended to
price watched securities. The guardrails then need no change — the sell rules
do not apply, and the weight cap, funded cash and coverage gates already do the
right thing for a new position.

Worth flagging when the owner asks for it: the analyst sees no price, no
weight and no cost basis by design. It assesses whether a stated thesis still
holds. "Should I buy this" is largely a valuation question, and this pipeline
would answer a different one.

## What you can do today

Read `references/pipeline.md` and walk the owner through what the candidate
path *would* execute, stage by stage, without running anything. That is a
legitimate answer to "how would this work", and it costs nothing.
