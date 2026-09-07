"""SQLite persistence for the portfolio ledger.

Open databases through :func:`open_db` or :func:`connect_db`, which apply
pending migrations. Raw ``sqlite3.connect`` is only for the rare case where
migration must be skipped.

Public API:
    open_db            -- open or create the database, migrated, and return it
    open_existing_db   -- open an existing database, refusing to create one
    connect_db         -- same, with migration optional
    ensure_account     -- upsert the account row and return its id
    upsert_security    -- insert or update a security and its themes
    load_securities    -- all securities, keyed by ticker
    insert_trade       -- record one buy or sell
    load_trades        -- trades in ledger order
    insert_cash_flow   -- record money in or out
    load_cash_flows    -- cash flows in ledger order
    save_prices        -- upsert closing prices
    save_fx_rate       -- upsert one currency pair's rate
    replace_feed_events -- refresh a security's future feed-sourced events
    save_events        -- insert events, ignoring exact duplicates
    load_events        -- events in a date window
    save_consensus     -- record consensus estimates as observed today
    consensus_history  -- a security's recorded estimates, newest first
    save_snapshot      -- store a point-in-time valuation and its positions
    latest_snapshot    -- most recent snapshot for an account, with positions
    latest_prices      -- most recent stored price per security

Example:
    from config import DB_PATH
    from store import open_db, load_securities

    conn = open_db(DB_PATH)
    securities = load_securities(conn)
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from db.migrations import apply_migrations
from models import (
    Account,
    CashFlow,
    ConsensusEstimate,
    Event,
    Security,
    Trade,
)

logger = logging.getLogger(__name__)


def _now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def connect_db(path: Path, *, migrate: bool = True) -> sqlite3.Connection:
    """Open or create the database at *path* and optionally migrate it.

    The parent directory is created if it does not exist.

    Args:
        path: Filesystem path for the SQLite database file.
        migrate: Whether to auto-apply pending migrations.

    Returns:
        An open connection with foreign keys enabled and WAL mode set.
    """
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    if migrate:
        apply_migrations(conn)
    logger.debug("Opened database: %s", path)
    return conn


def open_db(path: Path) -> sqlite3.Connection:
    """Open or create the database at *path* and apply migrations.

    Creates the file if it is missing, so only ``init`` should call this.
    Every read command must use :func:`open_existing_db` instead.
    """
    return connect_db(path, migrate=True)


def open_existing_db(path: Path) -> sqlite3.Connection:
    """Open an existing database at *path*, refusing to create one.

    ``sqlite3.connect`` happily creates an empty file, which turns a typo in
    ``--db`` or a wrong ``CASH_ASH_HOME`` into a silent empty portfolio that
    reports no holdings rather than an error. Read commands must not be able to
    do that.

    Args:
        path: Filesystem path for the SQLite database file.

    Returns:
        An open, migrated connection.

    Raises:
        FileNotFoundError: If no database exists at *path*.
    """
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(
            f"No database at {resolved}. Run 'uv run python main.py init' to "
            f"create and seed it."
        )
    return connect_db(resolved, migrate=True)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def ensure_account(conn: sqlite3.Connection, account: Account) -> int:
    """Insert the account if absent and return its id.

    Args:
        conn: Open database connection.
        account: Account to ensure exists, matched on ``name``.

    Returns:
        The account's row id.
    """
    with conn:
        conn.execute(
            """
            INSERT INTO accounts (name, broker, currency, sync_mode, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                broker = excluded.broker,
                currency = excluded.currency,
                sync_mode = excluded.sync_mode
            """,
            (
                account.name,
                account.broker,
                account.currency,
                account.sync_mode,
                _now(),
            ),
        )
    row = conn.execute(
        "SELECT id FROM accounts WHERE name = ?", (account.name,)
    ).fetchone()
    return int(row["id"])


# ---------------------------------------------------------------------------
# Securities
# ---------------------------------------------------------------------------


def upsert_security(conn: sqlite3.Connection, security: Security) -> int:
    """Insert or update a security and replace its theme tags.

    Themes are replaced wholesale rather than merged: they are a curated list,
    and a tag removed from the definition should disappear from the report.

    Args:
        conn: Open database connection.
        security: Security to store, matched on ``ticker``.

    Returns:
        The security's row id.
    """
    with conn:
        conn.execute(
            """
            INSERT INTO securities (
                ticker, name, currency, asset_class, pricing_mode,
                feed_symbol, sector, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                name = excluded.name,
                currency = excluded.currency,
                asset_class = excluded.asset_class,
                pricing_mode = excluded.pricing_mode,
                feed_symbol = excluded.feed_symbol,
                sector = excluded.sector
            """,
            (
                security.ticker,
                security.name,
                security.currency,
                security.asset_class,
                security.pricing_mode,
                security.feed_symbol,
                security.sector,
                _now(),
            ),
        )
        row = conn.execute(
            "SELECT id FROM securities WHERE ticker = ?", (security.ticker,)
        ).fetchone()
        security_id = int(row["id"])
        conn.execute(
            "DELETE FROM security_themes WHERE security_id = ?", (security_id,)
        )
        conn.executemany(
            "INSERT INTO security_themes (security_id, theme) VALUES (?, ?)",
            [(security_id, theme) for theme in security.themes],
        )
    return security_id


def load_securities(conn: sqlite3.Connection) -> dict[str, Security]:
    """Return every security keyed by ticker, themes included."""
    themes: dict[int, list[str]] = {}
    for row in conn.execute(
        "SELECT security_id, theme FROM security_themes ORDER BY theme"
    ):
        themes.setdefault(int(row["security_id"]), []).append(row["theme"])

    securities: dict[str, Security] = {}
    for row in conn.execute("SELECT * FROM securities ORDER BY ticker"):
        security_id = int(row["id"])
        securities[row["ticker"]] = Security(
            id=security_id,
            ticker=row["ticker"],
            name=row["name"],
            currency=row["currency"],
            asset_class=row["asset_class"],
            pricing_mode=row["pricing_mode"],
            feed_symbol=row["feed_symbol"],
            sector=row["sector"],
            themes=tuple(themes.get(security_id, ())),
        )
    return securities


# ---------------------------------------------------------------------------
# Trades and cash flows
# ---------------------------------------------------------------------------


def insert_trade(conn: sqlite3.Connection, trade: Trade) -> int:
    """Record one buy or sell and return its row id."""
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO trades (
                account_id, security_id, trade_date, side, quantity,
                price_native, fx_rate, amount_eur, fee_eur, is_synthetic,
                note, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.account_id,
                trade.security_id,
                trade.trade_date,
                trade.side,
                trade.quantity,
                trade.price_native,
                trade.fx_rate,
                trade.amount_eur,
                trade.fee_eur,
                int(trade.is_synthetic),
                trade.note,
                _now(),
            ),
        )
    return int(cursor.lastrowid)


def load_trades(
    conn: sqlite3.Connection, *, account_id: int | None = None
) -> list[Trade]:
    """Return trades oldest first, ties broken by insertion order.

    Args:
        conn: Open database connection.
        account_id: Restrict to one account, or None for all.

    Returns:
        Trades in the order they should be applied to a running position.
    """
    sql = "SELECT * FROM trades"
    params: tuple[object, ...] = ()
    if account_id is not None:
        sql += " WHERE account_id = ?"
        params = (account_id,)
    sql += " ORDER BY trade_date, id"
    return [
        Trade(
            id=int(row["id"]),
            account_id=int(row["account_id"]),
            security_id=int(row["security_id"]),
            trade_date=row["trade_date"],
            side=row["side"],
            quantity=row["quantity"],
            price_native=row["price_native"],
            fx_rate=row["fx_rate"],
            amount_eur=row["amount_eur"],
            fee_eur=row["fee_eur"],
            is_synthetic=bool(row["is_synthetic"]),
            note=row["note"],
        )
        for row in conn.execute(sql, params)
    ]


def insert_cash_flow(conn: sqlite3.Connection, flow: CashFlow) -> int:
    """Record money entering or leaving the account and return its row id."""
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO cash_flows (account_id, flow_date, kind, amount_eur, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                flow.account_id,
                flow.flow_date,
                flow.kind,
                flow.amount_eur,
                flow.note,
                _now(),
            ),
        )
    return int(cursor.lastrowid)


def load_cash_flows(
    conn: sqlite3.Connection, *, account_id: int | None = None
) -> list[CashFlow]:
    """Return cash flows oldest first."""
    sql = "SELECT * FROM cash_flows"
    params: tuple[object, ...] = ()
    if account_id is not None:
        sql += " WHERE account_id = ?"
        params = (account_id,)
    sql += " ORDER BY flow_date, id"
    return [
        CashFlow(
            id=int(row["id"]),
            account_id=int(row["account_id"]),
            flow_date=row["flow_date"],
            kind=row["kind"],
            amount_eur=row["amount_eur"],
            note=row["note"],
        )
        for row in conn.execute(sql, params)
    ]


# ---------------------------------------------------------------------------
# Snapshots and prices
# ---------------------------------------------------------------------------


def save_snapshot(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    snapshot_date: str,
    cash_eur: float,
    positions: list[tuple[int, float, float, float | None]],
    source: str,
    note: str | None = None,
) -> int:
    """Store a point-in-time valuation and its position rows.

    Replaces any existing snapshot for the same account, date and source, so
    re-seeding or re-syncing is idempotent.

    Args:
        conn: Open database connection.
        account_id: Account the snapshot belongs to.
        snapshot_date: ISO date of the valuation.
        cash_eur: Uninvested cash at that moment.
        positions: ``(security_id, quantity, value_eur, reported_return_pct)``.
        source: Where the figures came from, e.g. ``revolut``.
        note: Optional free text.

    Returns:
        The snapshot's row id.
    """
    positions_value = sum(value for _, _, value, _ in positions)
    with conn:
        conn.execute(
            """
            DELETE FROM portfolio_snapshots
            WHERE account_id = ? AND snapshot_date = ? AND source = ?
            """,
            (account_id, snapshot_date, source),
        )
        cursor = conn.execute(
            """
            INSERT INTO portfolio_snapshots (
                account_id, snapshot_date, total_value_eur, cash_eur,
                positions_value_eur, source, note, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                snapshot_date,
                positions_value + cash_eur,
                cash_eur,
                positions_value,
                source,
                note,
                _now(),
            ),
        )
        snapshot_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO snapshot_positions (
                snapshot_id, security_id, quantity, value_eur, reported_return_pct
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (snapshot_id, security_id, quantity, value, reported)
                for security_id, quantity, value, reported in positions
            ],
        )
    return snapshot_id


def latest_snapshot(
    conn: sqlite3.Connection, *, account_id: int
) -> tuple[sqlite3.Row, dict[int, sqlite3.Row]] | None:
    """Return the most recent snapshot and its positions keyed by security id.

    Args:
        conn: Open database connection.
        account_id: Account to look up.

    Returns:
        ``(snapshot_row, {security_id: position_row})``, or None if the account
        has no snapshots.
    """
    snapshot = conn.execute(
        """
        SELECT * FROM portfolio_snapshots
        WHERE account_id = ?
        ORDER BY snapshot_date DESC, id DESC
        LIMIT 1
        """,
        (account_id,),
    ).fetchone()
    if snapshot is None:
        return None
    rows = conn.execute(
        "SELECT * FROM snapshot_positions WHERE snapshot_id = ?",
        (snapshot["id"],),
    ).fetchall()
    return snapshot, {int(row["security_id"]): row for row in rows}


def latest_prices(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    """Return the most recent stored price per security, keyed by security id."""
    rows = conn.execute(
        """
        SELECT p.*
        FROM prices p
        JOIN (
            SELECT security_id, MAX(price_date) AS price_date
            FROM prices
            GROUP BY security_id
        ) latest
          ON latest.security_id = p.security_id
         AND latest.price_date = p.price_date
        """
    ).fetchall()
    return {int(row["security_id"]): row for row in rows}


def save_prices(
    conn: sqlite3.Connection,
    rows: list[tuple[int, str, float, str, str]],
) -> int:
    """Upsert closing prices.

    Re-fetching the same date overwrites rather than duplicating, so a sync run
    twice in one day is harmless.

    Args:
        conn: Open database connection.
        rows: ``(security_id, price_date, close_native, currency, source)``.

    Returns:
        The number of rows written.
    """
    if not rows:
        return 0
    now = _now()
    with conn:
        conn.executemany(
            """
            INSERT INTO prices (
                security_id, price_date, close_native, currency, source, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(security_id, price_date) DO UPDATE SET
                close_native = excluded.close_native,
                currency = excluded.currency,
                source = excluded.source,
                fetched_at = excluded.fetched_at
            """,
            [(*row, now) for row in rows],
        )
    return len(rows)


def save_fx_rate(
    conn: sqlite3.Connection,
    *,
    rate_date: str,
    base: str,
    quote: str,
    rate: float,
    source: str,
) -> None:
    """Upsert one currency pair's rate for one date.

    Args:
        conn: Open database connection.
        rate_date: ISO date the rate is for.
        base: Currency converted from.
        quote: Currency converted to.
        rate: How many *quote* units one *base* unit buys.
        source: Where the rate came from.

    Raises:
        ValueError: If the rate is not positive. A zero or negative rate would
            silently value the whole portfolio at nothing.
    """
    if rate <= 0:
        raise ValueError(
            f"Refusing to store a non-positive {base}/{quote} rate: {rate}."
        )
    with conn:
        conn.execute(
            """
            INSERT INTO fx_rates (rate_date, base, quote, rate, source, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(rate_date, base, quote) DO UPDATE SET
                rate = excluded.rate,
                source = excluded.source,
                fetched_at = excluded.fetched_at
            """,
            (rate_date, base, quote, rate, source, _now()),
        )


def save_events(conn: sqlite3.Connection, events: list[Event]) -> int:
    """Insert events, ignoring ones already recorded identically.

    Args:
        conn: Open database connection.
        events: Events to record.

    Returns:
        The number of rows actually inserted.
    """
    if not events:
        return 0
    now = _now()
    before = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    with conn:
        conn.executemany(
            # ON CONFLICT DO NOTHING, not INSERT OR IGNORE: the latter swallows
            # every constraint failure, so a bad source or confidence value
            # vanished silently instead of being rejected. This form skips only
            # a uniqueness collision and still raises on a CHECK violation.
            """
            INSERT INTO events (
                security_id, event_date, kind, title, source, confidence, note, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            [
                (
                    event.security_id,
                    event.event_date,
                    event.kind,
                    event.title,
                    event.source,
                    event.confidence,
                    event.note,
                    now,
                )
                for event in events
            ],
        )
    after = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    return after - before


def replace_feed_events(
    conn: sqlite3.Connection,
    *,
    security_id: int,
    kind: str,
    on_or_after: str,
) -> None:
    """Clear future feed-sourced events of one kind before re-fetching them.

    Companies move their reporting dates. Without this a rescheduled earnings
    call leaves the old date in place beside the new one, and triage sees two.
    Past events are never touched: what already happened is history, and the
    thesis that referenced it must still make sense.

    Args:
        conn: Open database connection.
        security_id: Security whose events to clear.
        kind: Event kind to clear.
        on_or_after: Only clear events on or after this ISO date.
    """
    with conn:
        conn.execute(
            """
            DELETE FROM events
            WHERE security_id = ? AND kind = ? AND source = 'feed'
              AND event_date >= ?
            """,
            (security_id, kind, on_or_after),
        )


def load_events(
    conn: sqlite3.Connection,
    *,
    start: str | None = None,
    end: str | None = None,
    security_id: int | None = None,
) -> list[sqlite3.Row]:
    """Return events in a date window, soonest first, with their tickers.

    Args:
        conn: Open database connection.
        start: Earliest ISO date, inclusive.
        end: Latest ISO date, inclusive.
        security_id: Restrict to one security.

    Returns:
        Event rows joined to their security's ticker, which is NULL for macro
        events belonging to no single holding.
    """
    clauses: list[str] = []
    params: list[object] = []
    if start:
        clauses.append("e.event_date >= ?")
        params.append(start)
    if end:
        clauses.append("e.event_date <= ?")
        params.append(end)
    if security_id is not None:
        clauses.append("e.security_id = ?")
        params.append(security_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"""
        SELECT e.*, s.ticker
        FROM events e
        LEFT JOIN securities s ON s.id = e.security_id
        {where}
        ORDER BY e.event_date, s.ticker
        """,
        params,
    ).fetchall()


def save_consensus(conn: sqlite3.Connection, estimates: list[ConsensusEstimate]) -> int:
    """Record consensus estimates as observed on their observation date.

    Re-running on the same day overwrites rather than duplicating, so a sync
    run twice is harmless and the series stays one point per day.

    Args:
        conn: Open database connection.
        estimates: Estimates to record.

    Returns:
        The number of rows written.
    """
    if not estimates:
        return 0
    now = _now()
    with conn:
        conn.executemany(
            """
            INSERT INTO consensus_estimates (
                security_id, observed_date, period_end, eps_avg, eps_low, eps_high,
                revenue_avg, revenue_low, revenue_high, source, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(security_id, observed_date) DO UPDATE SET
                period_end = excluded.period_end,
                eps_avg = excluded.eps_avg,
                eps_low = excluded.eps_low,
                eps_high = excluded.eps_high,
                revenue_avg = excluded.revenue_avg,
                revenue_low = excluded.revenue_low,
                revenue_high = excluded.revenue_high,
                source = excluded.source,
                fetched_at = excluded.fetched_at
            """,
            [
                (
                    estimate.security_id,
                    estimate.observed_date,
                    estimate.period_end,
                    estimate.eps_avg,
                    estimate.eps_low,
                    estimate.eps_high,
                    estimate.revenue_avg,
                    estimate.revenue_low,
                    estimate.revenue_high,
                    estimate.source,
                    now,
                )
                for estimate in estimates
            ],
        )
    return len(estimates)


def consensus_history(
    conn: sqlite3.Connection, *, security_id: int, limit: int = 30
) -> list[sqlite3.Row]:
    """Return a security's recorded estimates, newest observation first."""
    return conn.execute(
        """
        SELECT * FROM consensus_estimates
        WHERE security_id = ?
        ORDER BY observed_date DESC
        LIMIT ?
        """,
        (security_id, limit),
    ).fetchall()
