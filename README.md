# skarbie

AI-assisted research and decision support for a small personal stock portfolio.

Researches holdings on a weekly cadence, proposes constrained
`BUY / ADD / HOLD / TRIM / EXIT / REVIEW` actions, and keeps enough history to
judge those decisions later. Human-in-the-loop by design: the system recommends,
you approve, you execute manually in Revolut.

**Not built yet.** Today it is the portfolio ledger and market data — trades,
live valuation, FX, and concentration reporting. No AI yet. See
[docs/roadmap.md](docs/roadmap.md) for what comes next and why in that order.

## Quick start

```bash
# 1. Create your profile. --operator makes it the default for bare commands.
uv run python main.py profile add adam --telegram-id 123456789 --operator

# 2. Describe your holdings. This file never leaves your machine.
cp seed_snapshot.example.toml ~/Documents/skarbie/profiles/adam/seed_snapshot.toml
$EDITOR ~/Documents/skarbie/profiles/adam/seed_snapshot.toml

# 3. Check it reconciles against your broker's stated total, then seed.
uv run python main.py init --dry-run
uv run python main.py init

uv run python main.py sync             # fetch live prices and FX rates
uv run python main.py holdings         # positions, cost basis, P&L
uv run python main.py concentration    # weights by security, sector and theme
uv run python main.py doctor           # what is set up, what is still missing
```

`profile add` also writes starter context files — `investor.md`, `strategy.md`,
`log.md`, `watchlist.md` — under the profile's `context/` directory. Nothing
reads them yet; the research pipeline will, from Phase 4. `main.py context`
shows which are still untouched templates.

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

## Profiles

One profile is one person: their own database, broker snapshot, context files
and Telegram id. A household shares one bot — the token is infrastructure in
`.env`, and the roster's numeric `telegram_id` is what the bot routes messages
by, so nobody needs their own BotFather registration.

```
~/Documents/skarbie/
  profiles.toml                 the roster
  profiles/adam/
    portfolio.db
    seed_snapshot.toml          your holdings, never committed
    context/                    investor.md, strategy.md, log.md, watchlist.md
```

Every portfolio command takes `--profile NAME`; omitting it means the operator
profile. `--db PATH` bypasses the roster entirely and exists for experimental
databases.

## Configuration

Copy `.env_example` to `.env`. Every tunable — guardrails, defaults, tolerances
— is defined in `src/config.py` with a docstring explaining how its value was
chosen. Per-person settings such as the monthly contribution live in
`profiles.toml`, not in `config.py`. Set `SKARBIE_HOME` to move the whole app
directory.

## Development

```bash
uv run ruff check . && uv run ruff format .
uv run pytest
```

Conventions: [CLAUDE.md](CLAUDE.md) (identical to `AGENTS.md`).
