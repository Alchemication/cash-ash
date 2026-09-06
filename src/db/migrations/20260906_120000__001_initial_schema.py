"""Portfolio ledger: accounts, securities, trades, cash flows, prices, snapshots."""

from __future__ import annotations

import sqlite3

NAME = "initial schema"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create the portfolio ledger.

    Positions are deliberately *not* a table. A position is the sum of its
    trades, and storing it alongside them creates two sources of truth that
    drift the first time a trade is corrected — which the seed data guarantees
    will happen, since it is a broker screenshot rather than a real history.
    ``portfolio.positions()`` derives them instead.

    ``accounts`` carries a single row in v1. It exists so that a second broker
    is an INSERT rather than a migration of every valuation query; it adds no
    commands and no concepts until there is something to put in it.

    Money is stored in EUR because that is what the broker reports and what is
    actually gained or lost. Prices are stored in each security's native
    currency with the FX rate kept separately, so a return can be split into
    stock move and currency move. The broker's own percentage blends the two,
    which is why it seeds the book but is never read back as an input.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id          INTEGER PRIMARY KEY,
            name        TEXT NOT NULL UNIQUE,
            broker      TEXT NOT NULL,
            currency    TEXT NOT NULL,
            sync_mode   TEXT NOT NULL CHECK (sync_mode IN ('manual', 'adapter')),
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS securities (
            id            INTEGER PRIMARY KEY,
            ticker        TEXT NOT NULL UNIQUE,
            name          TEXT NOT NULL,
            currency      TEXT NOT NULL,
            asset_class   TEXT NOT NULL DEFAULT 'equity'
                          CHECK (asset_class IN ('equity', 'fund', 'crypto')),
            pricing_mode  TEXT NOT NULL DEFAULT 'feed'
                          CHECK (pricing_mode IN ('feed', 'manual')),
            feed_symbol   TEXT,
            sector        TEXT,
            created_at    TEXT NOT NULL
        );

        -- Many-to-many on purpose: NVDA is both a mega-cap and an AI
        -- semiconductor, and forcing one label would hide whichever
        -- concentration the user cared about. Theme weights may therefore sum
        -- past 100%, which is correct rather than a bug.
        CREATE TABLE IF NOT EXISTS security_themes (
            security_id  INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
            theme        TEXT NOT NULL,
            PRIMARY KEY (security_id, theme)
        );

        CREATE TABLE IF NOT EXISTS cash_flows (
            id          INTEGER PRIMARY KEY,
            account_id  INTEGER NOT NULL REFERENCES accounts(id),
            flow_date   TEXT NOT NULL,
            kind        TEXT NOT NULL CHECK (kind IN (
                            'OPENING_BALANCE', 'CONTRIBUTION', 'WITHDRAWAL',
                            'DIVIDEND', 'FEE'
                        )),
            amount_eur  REAL NOT NULL,
            note        TEXT,
            created_at  TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS cash_flows_date ON cash_flows(flow_date);

        CREATE TABLE IF NOT EXISTS trades (
            id            INTEGER PRIMARY KEY,
            account_id    INTEGER NOT NULL REFERENCES accounts(id),
            security_id   INTEGER NOT NULL REFERENCES securities(id),
            trade_date    TEXT NOT NULL,
            side          TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
            quantity      REAL NOT NULL CHECK (quantity > 0),
            -- Null for synthetic opening trades: the seed snapshot reports a
            -- EUR value and a blended return, never a per-share price or the
            -- rate it was struck at.
            price_native  REAL,
            fx_rate       REAL,
            amount_eur    REAL NOT NULL,
            fee_eur       REAL NOT NULL DEFAULT 0,
            is_synthetic  INTEGER NOT NULL DEFAULT 0,
            note          TEXT,
            created_at    TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS trades_security ON trades(security_id, trade_date);

        CREATE TABLE IF NOT EXISTS prices (
            security_id   INTEGER NOT NULL REFERENCES securities(id),
            price_date    TEXT NOT NULL,
            close_native  REAL NOT NULL,
            currency      TEXT NOT NULL,
            source        TEXT NOT NULL,
            fetched_at    TEXT NOT NULL,
            PRIMARY KEY (security_id, price_date)
        );

        CREATE TABLE IF NOT EXISTS fx_rates (
            rate_date   TEXT NOT NULL,
            base        TEXT NOT NULL,
            quote       TEXT NOT NULL,
            rate        REAL NOT NULL,
            source      TEXT NOT NULL,
            fetched_at  TEXT NOT NULL,
            PRIMARY KEY (rate_date, base, quote)
        );

        -- A point-in-time valuation, kept because the ledger alone cannot
        -- reproduce one: it would need every historical price, and the seed
        -- date has none at all.
        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id                  INTEGER PRIMARY KEY,
            account_id          INTEGER NOT NULL REFERENCES accounts(id),
            snapshot_date       TEXT NOT NULL,
            total_value_eur     REAL NOT NULL,
            cash_eur            REAL NOT NULL,
            positions_value_eur REAL NOT NULL,
            source              TEXT NOT NULL,
            note                TEXT,
            created_at          TEXT NOT NULL,
            UNIQUE (account_id, snapshot_date, source)
        );

        CREATE TABLE IF NOT EXISTS snapshot_positions (
            snapshot_id          INTEGER NOT NULL
                                 REFERENCES portfolio_snapshots(id) ON DELETE CASCADE,
            security_id          INTEGER NOT NULL REFERENCES securities(id),
            quantity             REAL NOT NULL,
            value_eur            REAL NOT NULL,
            -- The broker's own blended return, kept for provenance and for
            -- reconciliation against the derived cost basis. Never an input to
            -- a decision: it mixes stock performance with EUR/USD.
            reported_return_pct  REAL,
            PRIMARY KEY (snapshot_id, security_id)
        );
        """
    )
