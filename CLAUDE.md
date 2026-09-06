## Rules

Always use `uv run`, never plain `python`. Full command list: `docs/commands.md` (or `--help`).

Open the database via `store.open_db()` or `store.connect_db(..., migrate=True)` —
these auto-apply pending migrations. Use raw `sqlite3.connect()` only when you
specifically need to skip migration. Only `main.py init` may create a database;
every other command must fail with a message telling the user to run it, rather
than silently creating an empty one.

Schema changes go in new timestamped migration files under `src/db/migrations/`.
No ad-hoc `ALTER TABLE`, column-existence checks, or schema-patching in
application code.

**Positions are derived, never stored.** A position is the sum of its trades;
`portfolio.positions()` computes it. The seed data is a broker screenshot, so
trades *will* be corrected, and a stored position would drift from them the
first time that happens.

**Money is stored in EUR; prices are stored in native currency.** The FX rate
lives in `fx_rates`, separately, so a return can be split into stock move and
currency move. The broker's own blended return percentage seeds the book once
and is never read back as an input to a decision.

**A holding that cannot be priced reports `None`, never `0.0`.** An unpriced
instrument must be visibly missing from a total, not silently shrink it. Every
current holding is feed-priceable, so this path is exercised by tests rather
than by production data — which is exactly why it must not be allowed to rot: a
feed outage, a delisting or a halt turns it on with no warning. `pricing_mode`
exists for the same reason. The research pipeline needs the equivalent escape:
it must be able to skip a security rather than assume all holdings can be
analysed alike.

**No personal data in the repository.** Holdings, quantities, euro amounts,
Telegram ids and API keys live under `SKARBIE_HOME`, never in tracked files.
Git history is permanent, so a value committed once is committed forever. The
repository ships `.env_example` and `seed_snapshot.example.toml` with invented
figures; tests use synthetic fixtures under `tests/fixtures/`.

Operational tunables — thresholds, limits, guardrails, windows — live in
`src/config.py` as named constants, each with a docstring saying how the value
was chosen. Never inline them at the point of use, and never bury one in a
defaults dict.

Natural-language LLM prompts live in `src/prompts/`; keep tool schemas beside
tool code.

Work on `main` and push straight to it — no feature branches, no PRs. Single
developer for now. Still commit only when asked, and only with lint and tests
green.

## Collaboration Style

Challenge my ideas early. If an approach is over-engineered, fragile, or has a
simpler alternative — say so directly with reasoning. Flag knowledge gaps,
hidden trade-offs, or narrowed thinking. Be pragmatic; save me from wasting
time on something that could be done better.

**Prose:** terse, no pleasantries / hedging / filler. Fragments fine. Full
sentences only for warnings, destructive-op confirmations, and commit/PR
messages.

**Verification:** After cross-cutting changes (multiple modules, interface
changes, file moves), verify before reporting done. Grep for stale references,
check callers of changed functions, confirm imports, run lint + tests, and fix
what you find — don't wait to be asked.

## This is a learning project, and the numbers say so

The portfolio is ~EUR 1,400 across 14 positions. A weekly multi-model research
pipeline can easily cost more per year than any excess return it could earn on
that capital. That is an accepted trade — the goal is better research habits and
an interesting system, not alpha. Two consequences that are not optional:

- Model routing defaults cheap; expensive models are opt-in and measured.
- Never present model output as if it carried investment authority. An LLM
  "confidence: 91%" is not a calibrated probability and must never be stored or
  rendered as one.

Outcome evals are statistically dead on arrival at this scale — 14 tickers a
week will never distinguish one model's stock picking from another's, and
historical replay leaks the future through the model's own weights. Evals
measure *process*: schema validity, sourced claims, guardrails respected, no
fabricated figures, stability across reruns. Forward-tracked outcomes and the
passive benchmark are recorded, never claimed as evidence.

## Documentation

Any change to user-visible behavior, commands, defaults, limits, paths, model
routing, storage, or workflows must audit and update the relevant `README.md`,
`docs/*.md`, and `.env_example` in the same commit, even when the code change is
small.

Docs describe current behavior, not implementation history or future plans —
except `docs/roadmap.md`, which is explicitly about what is not built yet.

Docs name the knob and the command that prints its value; code owns the value.
A literal value earns a place in the docs only when it is a user-facing default
someone configures against, and then it appears once.

Keep `AGENTS.md` and `CLAUDE.md` identical. When editing either, update both.

## Code Style

- **Linter/formatter:** `uv run ruff check .` and `uv run ruff format .`
- **Type hints:** required on all signatures. Native types only (`list`, `dict`, `str | None`) — never `typing.List` etc.
- **Docstrings:** Google style.
- **File size:** keep source files under ~1000 lines. If a module grows past that, extract a cohesive subset into its own file (see `cmd_db.py`).
- **No backward-compat shims:** when moving code, update all callers to import from the new location. No re-export stubs.

## Output Rules

- `print()` for user-facing content (reports, JSON) → stdout.
- `logger` (stdlib `logging`) for diagnostics, progress, errors → stderr.
- `rich` (import lazily inside the handler) for structured terminal display.
- Error messages should tell the user what to do, not just what went wrong.

## Testing

`uv run pytest`. Fixtures in `tests/conftest.py`.

**Must have tests:** anything computing money — position derivation, cost basis,
cash, valuation fallbacks, concentration weights — plus store round-trips and
the seed reconciliation.

**Style:** group in classes (`class TestPositions`); use the `conn` fixture for
an in-memory migrated database; cover edge cases that would silently break
(None, zero totals, over-sells, missing FX); run ruff on `tests/` before
committing.

## Code map

`main.py` is dispatch only — subcommand handlers live in `src/commands.py` plus
`src/cmd_*.py`, split as `commands.py` approaches ~1000 lines.

Layers, bottom up:

- `src/db/migrations/` — versioned schema, one file per change
- `src/store.py` — SQLite read/write, no business logic
- `src/models.py` — plain dataclasses, no persistence
- `src/portfolio.py` — derived positions, valuation, concentration
- `src/seed.py` — the 2026-09-02 Revolut snapshot and its cost-basis derivation
- `src/commands.py`, `src/cmd_*.py` — CLI handlers and rendering

`accounts` holds a single row. It exists so a second broker is an INSERT rather
than a rewrite of every valuation query; do not build multi-account features on
it until there is a second account to justify them.
