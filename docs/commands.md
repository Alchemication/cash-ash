# Commands

Always use `uv run`. Run any command with `--help` for the full flag list.

```bash
uv run python main.py init             # create and seed from the snapshot file
uv run python main.py holdings         # positions, cost basis, value, P&L
uv run python main.py concentration    # grouped weights and limit breaches
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
```

`--db PATH` overrides the database location on any command; `SKARBIE_HOME`
moves the whole app directory. Only `init` creates a database — everything else
fails with a message pointing at it, so a typo in a path cannot silently produce
an empty portfolio.

## Reading the output

`holdings` values each position at the best price available: a stored market
price converted at the stored FX rate, else the per-unit value implied by the
most recent snapshot, else unknown. The footer names the date the prices came
from and lists anything that could not be priced. Unpriced holdings are excluded
from the total and from every weight.

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
