# Commands

Always use `uv run`. Run any command with `--help` for the full flag list.

```bash
uv run python main.py profile add NAME --telegram-id ID --operator   # first profile
uv run python main.py profile list     # the roster
uv run python main.py init             # create and seed from the snapshot file
uv run python main.py sync             # fetch current prices and FX rates
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
