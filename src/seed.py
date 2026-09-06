"""Seed the database from a broker snapshot file.

The snapshot is a screenshot, not a transaction history: it reports a quantity,
a EUR value and a blended return per position, and nothing else. Cost basis is
therefore *derived* as ``value / (1 + return)`` and written as one synthetic
opening BUY per position, flagged so it is never mistaken for a real trade.

The snapshot file holds real holdings and euro amounts, so it lives under the
app home rather than in the repository — see ``config.SEED_SNAPSHOT_PATH``.
``seed_snapshot.example.toml`` in the project root shows the format with
invented figures.

Public API:
    load_snapshot   -- parse and validate a snapshot TOML file
    reconcile       -- derived totals against the broker's stated ones
    seed_database   -- create the account, securities, opening trades, snapshot
    SeedSnapshot    -- a parsed snapshot
    SeedPosition    -- one row of it

Example:
    from config import DB_PATH, SEED_SNAPSHOT_PATH
    from seed import load_snapshot, seed_database
    from store import open_db

    seed_database(open_db(DB_PATH), load_snapshot(SEED_SNAPSHOT_PATH))
"""

from __future__ import annotations

import logging
import sqlite3
import tomllib
from dataclasses import dataclass
from pathlib import Path

from models import Account, CashFlow, Security, Trade
from store import (
    ensure_account,
    insert_cash_flow,
    insert_trade,
    save_snapshot,
    upsert_security,
)

logger = logging.getLogger(__name__)

_REQUIRED_POSITION_FIELDS = ("ticker", "name", "quantity", "value_eur")


@dataclass(frozen=True)
class SeedPosition:
    """One row of a broker snapshot, plus the classification we add.

    Attributes:
        ticker: Canonical symbol as the user knows it.
        name: Human-readable company name.
        quantity: Units held, fractional shares included.
        value_eur: Market value the broker displayed.
        reported_return_pct: The broker's blended return. Mixes stock move with
            FX, so it derives cost basis once and is never read back as an
            input to a decision.
        currency: Native listing currency.
        sector: Broad sector label for concentration reporting.
        themes: Overlapping theme labels.
        feed_symbol: Market-data symbol when it differs from the ticker.
        pricing_mode: ``feed`` or ``manual``.
    """

    ticker: str
    name: str
    quantity: float
    value_eur: float
    reported_return_pct: float = 0.0
    currency: str = "USD"
    sector: str | None = None
    themes: tuple[str, ...] = ()
    feed_symbol: str | None = None
    pricing_mode: str = "feed"

    @property
    def cost_basis_eur(self) -> float:
        """EUR paid, derived from the value and the broker's blended return."""
        return self.value_eur / (1 + self.reported_return_pct / 100)


@dataclass(frozen=True)
class SeedSnapshot:
    """A parsed broker snapshot ready to seed a database."""

    date: str
    source: str
    cash_eur: float
    positions: tuple[SeedPosition, ...]
    reported_total_eur: float | None = None
    account_name: str = "revolut"
    account_broker: str = "Revolut"
    account_currency: str = "EUR"

    @property
    def account(self) -> Account:
        """The account this snapshot belongs to."""
        return Account(
            name=self.account_name,
            broker=self.account_broker,
            currency=self.account_currency,
            sync_mode="manual",
        )


def load_snapshot(path: Path) -> SeedSnapshot:
    """Parse and validate a snapshot TOML file.

    Args:
        path: Path to the snapshot file.

    Returns:
        The parsed snapshot.

    Raises:
        FileNotFoundError: If the file does not exist, with instructions for
            creating one from the shipped example.
        ValueError: If a required key is missing or a position is malformed.
    """
    resolved = path.expanduser()
    if not resolved.exists():
        raise FileNotFoundError(
            f"No snapshot at {resolved}. Copy seed_snapshot.example.toml there "
            f"and replace the example figures with your broker's, then run "
            f"'uv run python main.py init' again. The file stays on your "
            f"machine; it is never committed."
        )

    with resolved.open("rb") as handle:
        raw = tomllib.load(handle)

    for key in ("date", "cash_eur"):
        if key not in raw:
            raise ValueError(f"{resolved}: missing required key '{key}'.")

    rows = raw.get("position", [])
    if not rows:
        raise ValueError(f"{resolved}: no [[position]] entries found.")

    positions: list[SeedPosition] = []
    for index, row in enumerate(rows, start=1):
        missing = [key for key in _REQUIRED_POSITION_FIELDS if key not in row]
        if missing:
            raise ValueError(
                f"{resolved}: position {index} is missing {', '.join(missing)}."
            )
        if row["quantity"] <= 0:
            raise ValueError(
                f"{resolved}: position {index} ({row['ticker']}) has a "
                f"non-positive quantity."
            )
        positions.append(
            SeedPosition(
                ticker=row["ticker"],
                name=row["name"],
                quantity=float(row["quantity"]),
                value_eur=float(row["value_eur"]),
                reported_return_pct=float(row.get("reported_return_pct", 0.0)),
                currency=row.get("currency", "USD"),
                sector=row.get("sector"),
                themes=tuple(row.get("themes", ())),
                feed_symbol=row.get("feed_symbol"),
                pricing_mode=row.get("pricing_mode", "feed"),
            )
        )

    duplicates = sorted(
        {
            position.ticker
            for position in positions
            if [p.ticker for p in positions].count(position.ticker) > 1
        }
    )
    if duplicates:
        raise ValueError(
            f"{resolved}: duplicate tickers {duplicates}. Merge them into one "
            f"[[position]] entry."
        )

    return SeedSnapshot(
        date=raw["date"],
        source=raw.get("source", "broker"),
        cash_eur=float(raw["cash_eur"]),
        positions=tuple(positions),
        reported_total_eur=(
            float(raw["reported_total_eur"]) if "reported_total_eur" in raw else None
        ),
        account_name=raw.get("account_name", "revolut"),
        account_broker=raw.get("account_broker", "Revolut"),
        account_currency=raw.get("account_currency", "EUR"),
    )


def reconcile(snapshot: SeedSnapshot) -> dict[str, float | None]:
    """Return the snapshot's derived totals against the broker's stated ones.

    Used by ``init`` and by tests to prove the cost-basis derivation is sound
    before anything is written.

    Args:
        snapshot: The parsed snapshot.

    Returns:
        Derived and reported totals plus their difference, all in EUR.
        ``reported_total_eur`` and ``total_difference_eur`` are None when the
        snapshot did not state a total.
    """
    market_value = sum(position.value_eur for position in snapshot.positions)
    cost = sum(position.cost_basis_eur for position in snapshot.positions)
    total = market_value + snapshot.cash_eur
    return {
        "positions_value_eur": market_value,
        "cash_eur": snapshot.cash_eur,
        "total_eur": total,
        "reported_total_eur": snapshot.reported_total_eur,
        "total_difference_eur": (
            total - snapshot.reported_total_eur
            if snapshot.reported_total_eur is not None
            else None
        ),
        "cost_basis_eur": cost,
        "unrealised_gain_eur": market_value - cost,
        "unrealised_return_pct": (market_value - cost) / cost * 100 if cost else 0.0,
    }


def seed_database(conn: sqlite3.Connection, snapshot: SeedSnapshot) -> int:
    """Populate a database from a parsed broker snapshot.

    Idempotent: securities are upserted and the snapshot replaces any earlier
    one for the same date and source, so re-running corrects classifications in
    place. Opening trades and the opening cash balance are written only when
    the ledger is empty, so re-running cannot double the book after real trades
    have been added.

    Args:
        conn: Open, migrated database connection.
        snapshot: The parsed snapshot to seed from.

    Returns:
        The account id.
    """
    account_id = ensure_account(conn, snapshot.account)

    security_ids: dict[str, int] = {}
    for position in snapshot.positions:
        security_ids[position.ticker] = upsert_security(
            conn,
            Security(
                ticker=position.ticker,
                name=position.name,
                currency=position.currency,
                asset_class="equity",
                pricing_mode=position.pricing_mode,
                feed_symbol=position.feed_symbol,
                sector=position.sector,
                themes=position.themes,
            ),
        )

    already_seeded = conn.execute(
        "SELECT 1 FROM trades WHERE account_id = ? LIMIT 1", (account_id,)
    ).fetchone()

    if already_seeded is None:
        opening_cost = sum(position.cost_basis_eur for position in snapshot.positions)
        insert_cash_flow(
            conn,
            CashFlow(
                account_id=account_id,
                flow_date=snapshot.date,
                kind="OPENING_BALANCE",
                amount_eur=opening_cost + snapshot.cash_eur,
                note=(
                    "Derived opening balance: cost basis of all seeded positions "
                    f"plus EUR {snapshot.cash_eur:.2f} reported cash."
                ),
            ),
        )
        for position in snapshot.positions:
            insert_trade(
                conn,
                Trade(
                    account_id=account_id,
                    security_id=security_ids[position.ticker],
                    trade_date=snapshot.date,
                    side="BUY",
                    quantity=position.quantity,
                    amount_eur=position.cost_basis_eur,
                    is_synthetic=True,
                    note=(
                        "Synthetic opening position. Cost basis derived from the "
                        f"{snapshot.source} snapshot value and its reported "
                        f"{position.reported_return_pct:+.2f}% return; no price "
                        "or FX rate was available."
                    ),
                ),
            )
        logger.info("Seeded %d opening positions", len(snapshot.positions))
    else:
        logger.info("Ledger already has trades; skipped opening trades and cash")

    save_snapshot(
        conn,
        account_id=account_id,
        snapshot_date=snapshot.date,
        cash_eur=snapshot.cash_eur,
        positions=[
            (
                security_ids[position.ticker],
                position.quantity,
                position.value_eur,
                position.reported_return_pct,
            )
            for position in snapshot.positions
        ],
        source=snapshot.source,
        note=f"Seed snapshot loaded from a {snapshot.source} export.",
    )
    return account_id
