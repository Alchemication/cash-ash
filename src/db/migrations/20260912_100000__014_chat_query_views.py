"""Views the chat agent can query without re-deriving anything.

A read-only SQL tool handed the raw tables would have to rebuild the ledger
arithmetic itself, one query at a time, and would get it wrong in ways nobody
notices: a cash balance that forgets to subtract buy fees looks entirely
plausible. These views move the arithmetic that *is* pure aggregation into the
schema, so the agent and ``portfolio.py`` read the same definition rather than
two implementations that agree until they don't.

Only aggregation lives here. Position cost basis deliberately does not: average
cost releases basis in proportion to the units held *at that trade*, which is
path-dependent and would need a recursive CTE to express. That stays in
``portfolio.positions`` and reaches the agent as a tool, because a second money
implementation is worse than no view at all.
"""

import sqlite3

NAME = "views for chat SQL: ticker-joined ledger, cash balance, latest prices"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create the read-only query views. No table or column is changed."""
    conn.executescript(
        """
        -- Trades with the ticker spelled out. The agent otherwise has to guess
        -- its way through securities.id joins on every single question, and a
        -- wrong join silently returns another holding's trades.
        CREATE VIEW IF NOT EXISTS v_trades AS
        SELECT
            t.id,
            t.account_id,
            t.trade_date,
            s.ticker,
            s.name          AS security_name,
            s.currency      AS security_currency,
            s.sector,
            s.asset_class,
            t.side,
            t.quantity,
            t.price_native,
            t.fx_rate,
            t.amount_eur,
            t.fee_eur,
            t.is_synthetic,
            t.note
        FROM trades t
        JOIN securities s ON s.id = t.security_id;

        -- Cash flows with nothing to join, present for symmetry: an agent that
        -- found v_trades will look for this name before it looks for the table.
        CREATE VIEW IF NOT EXISTS v_cash_flows AS
        SELECT id, account_id, flow_date, kind, amount_eur, note
        FROM cash_flows;

        -- Every movement of EUR in one column, so a balance is a SUM and a
        -- statement is an ORDER BY. Signs are applied here once: a buy costs
        -- its amount plus its fee, a sell returns its amount less its fee.
        CREATE VIEW IF NOT EXISTS v_cash_ledger AS
        SELECT
            account_id,
            flow_date           AS entry_date,
            kind,
            NULL                AS ticker,
            amount_eur          AS delta_eur
        FROM cash_flows
        UNION ALL
        SELECT
            t.account_id,
            t.trade_date        AS entry_date,
            t.side              AS kind,
            s.ticker,
            CASE t.side
                WHEN 'BUY' THEN -(t.amount_eur + t.fee_eur)
                ELSE              (t.amount_eur - t.fee_eur)
            END                 AS delta_eur
        FROM trades t
        JOIN securities s ON s.id = t.security_id;

        -- One row per account that has ever moved money. An account with no
        -- ledger at all is absent rather than zero, so callers coalesce.
        CREATE VIEW IF NOT EXISTS v_cash_balance AS
        SELECT account_id, SUM(delta_eur) AS cash_eur
        FROM v_cash_ledger
        GROUP BY account_id;

        -- Latest stored close per security. Not a valuation: it is native
        -- currency, unconverted, and a security with no price is simply
        -- absent. Converting and handling that absence is the part that must
        -- not be done in SQL.
        CREATE VIEW IF NOT EXISTS v_latest_price AS
        SELECT
            s.ticker,
            p.security_id,
            p.price_date,
            p.close_native,
            p.currency,
            p.source
        FROM prices p
        JOIN securities s ON s.id = p.security_id
        WHERE p.price_date = (
            SELECT MAX(p2.price_date) FROM prices p2
            WHERE p2.security_id = p.security_id
        );
        """
    )
