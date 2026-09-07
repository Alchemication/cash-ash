# Roadmap

Work not built yet, in priority order.

## Evidence quality

Automated company filings and earnings-release retrieval, query-aware search,
and an evaluation set checking whether cited excerpts actually support claims.
Current retrieval combines question-linked local excerpts with recent news;
mechanical citation checks establish membership and exact quotes only.

## Workflow depth

Broker transaction import, reconciliation and multiple partial fills. Execution
currently records one fill per approved recommendation. A guided thesis editor
and investment-attractiveness worksheet could make assumptions easier to revisit.

## Process evaluations

Extend regression tests and observed process metrics with frozen synthetic
research cases, repeated-run stability and reviewer-labelled claim support.
Measure time spent reviewing and whether questions were resolved. Outcomes and
the passive benchmark remain descriptive; they do not establish model skill.

Evaluation must be forward-only. Replaying a past week cannot validate a model
whose training already contains what happened next, so a favourable historical
result measures leakage rather than judgement. Frozen cases test process —
schema validity, sourced claims, guardrails respected, stability across reruns
— and never whether a call turned out well.

## Model experiments

Multiple neutral analysts are an experiment after the single-analyst workflow
has measured weaknesses. Compare process quality on identical evidence; do not
add providers or adversarial roles merely to produce more opinions. Defaults
remain cheap, with stronger models selected explicitly.

Neutral is deliberate. Assigning a model to argue a side manufactures the
argument, and a bear case produced on request is indistinguishable from one the
evidence forced — which destroys the only signal a panel offers. Disagreement
is worth something when it emerges between models reading the same frozen
evidence with the same instructions; it is worth nothing when it was assigned.
That is also why analyst temperature is zero: disagreement traceable to
sampling noise cannot be told apart from disagreement traceable to judgement.

## Benchmark methodology

Audit the synthetic opening balance, cash-flow timing and common valuation date.
The current percentage is gain against net contributed capital, not annualized
XIRR. Formal money-weighted returns and transaction-cost assumptions need a
separate implementation and tests before being presented as such.
