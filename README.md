# skarbie

AI-assisted research and decision support for a small personal stock portfolio.

Researches holdings on a weekly cadence, proposes constrained
`BUY / ADD / HOLD / TRIM / EXIT / REVIEW` actions, and keeps enough history to
judge those decisions later. Human-in-the-loop by design: the system recommends,
you approve, you execute manually in Revolut.

**Not built yet.** Today it is the portfolio ledger — accounts, trades,
valuation and concentration. See [docs/roadmap.md](docs/roadmap.md) for what
comes next and why in that order.

## Quick start

```bash
cp seed_snapshot.example.toml ~/Documents/skarbie/seed_snapshot.toml
$EDITOR ~/Documents/skarbie/seed_snapshot.toml   # your holdings; never committed
uv run python main.py init --dry-run   # check it reconciles, write nothing
uv run python main.py init             # create and seed the database
uv run python main.py holdings         # positions, cost basis, P&L
uv run python main.py concentration    # weights by security, sector and theme
```

Full command list: [docs/commands.md](docs/commands.md).

## How the book is kept

The database is seeded from a broker snapshot you write by hand. That snapshot
is a screenshot, not a transaction history: it gives a quantity, a EUR value and
a blended return per position, and nothing else. Cost basis is derived as
`value / (1 + return)` and written as one synthetic opening trade per position,
flagged as such. `init` prints the reconciliation and refuses to write if the
derived total drifts from the broker's stated total by more than the tolerance,
so a mistyped figure cannot quietly become your cost basis.

**Personal data never enters the repository.** The snapshot holds real holdings
and euro amounts, and git history is permanent, so it lives under
`~/Documents/skarbie/` beside the database it produces. The repository ships
only `seed_snapshot.example.toml` with invented figures. Copy it across and
fill in your own.

Three rules follow from that, and everything else depends on them:

- **Positions are derived from trades, never stored.** Correct a trade and every
  figure updates. Nothing to re-sync.
- **Money is EUR, prices are native currency, FX is stored separately.** The
  broker's blended percentage mixes stock performance with EUR/USD, so it seeds
  the book once and is never used as an input to a decision.
- **Unpriceable holdings report unknown, not zero.** Everything currently held
  can be priced by a feed, but an outage, a halt or a delisting turns that off
  without warning. Such a holding is excluded from totals and weights and the
  shortfall is printed, rather than silently valuing it at zero.

## Configuration

Copy `.env_example` to `.env`. Every tunable — guardrails, the planning
contribution, paths — is defined in `src/config.py` with a docstring explaining
how its value was chosen. The database lives at `~/Documents/skarbie/` by
default; set `SKARBIE_HOME` to move it.

## Development

```bash
uv run ruff check . && uv run ruff format .
uv run pytest
```

Conventions: [CLAUDE.md](CLAUDE.md) (identical to `AGENTS.md`).
