"""What following the recommendations would have done.

Every recommendation is stored with the price and the FX rate that stood behind
it, so "what if I had done what it said" is arithmetic over rows that already
exist. Nothing here is a claim about skill: a handful of trades cannot separate
judgement from luck, so every total is shown beside the number of trades it
rests on and is never labelled good or bad.

Following is read from the ledger rather than asked about. A trade in the same
security and direction within a few days of a recommendation counts as having
followed it, which makes the record a by-product of syncing the broker — no
buttons, no second workflow to keep in step with reality.

Keeping cash is a recommendation too, and for now it is the only one the system
makes, so it is measured as well: the cash on hand against what the benchmark
did over the same days.

Public API:
    ShadowTrade   -- one trade recommendation and what following it is worth now
    CashAdvice    -- what keeping cash has cost or saved since that advice
    Record        -- the window, its trades and its totals
    shadow_record -- build the record
    record_lines  -- render it as plain lines, for the report and the CLI

Example:
    from record import record_lines, shadow_record

    for line in record_lines(shadow_record(conn)):
        print(line)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from config import BENCHMARK_TICKER, RECORD_FOLLOW_WINDOW_DAYS, RECORD_WINDOW_WEEKS

_TRADE_ACTIONS = ("BUY", "ADD", "TRIM", "EXIT")
_BUY_ACTIONS = frozenset({"BUY", "ADD"})


@dataclass(frozen=True)
class ShadowTrade:
    """One trade recommendation, and what following it would be worth now.

    Attributes:
        ticker: Security the recommendation concerned.
        action: BUY, ADD, TRIM or EXIT.
        run_date: When it was recommended.
        amount_eur: What it proposed trading.
        followed: Whether a matching fill appeared in the ledger in time.
        delta_eur: Gain or loss from having followed it, or None when either
            price is missing. A sale's gain is the fall it avoided.
        delta_pct: The same as a percentage of the amount, or None.
    """

    ticker: str
    action: str
    run_date: str
    amount_eur: float
    followed: bool
    delta_eur: float | None
    delta_pct: float | None


@dataclass(frozen=True)
class CashAdvice:
    """What keeping cash has cost or saved since the advice to keep it.

    Attributes:
        since: Date of the most recent keep-cash recommendation.
        cash_eur: Cash on hand now.
        benchmark_move_pct: How the benchmark moved over those days, or None.
        cost_eur: Positive when keeping cash cost money, negative when it saved
            it, or None when the benchmark cannot be priced.
    """

    since: str
    cash_eur: float
    benchmark_move_pct: float | None
    cost_eur: float | None


@dataclass(frozen=True)
class Record:
    """The record over one window.

    Attributes:
        since: First day of the window.
        trades: Trade recommendations inside it, oldest first.
        cash: The standing keep-cash advice, if there is one.
        unpriced: Trades that could not be valued, left out of every total.
    """

    since: str
    trades: tuple[ShadowTrade, ...]
    cash: CashAdvice | None
    unpriced: int

    @property
    def followed(self) -> tuple[ShadowTrade, ...]:
        """Recommendations the ledger shows were acted on."""
        return tuple(trade for trade in self.trades if trade.followed)

    @property
    def skipped(self) -> tuple[ShadowTrade, ...]:
        """Recommendations that were not acted on."""
        return tuple(trade for trade in self.trades if not trade.followed)


def total_eur(trades: tuple[ShadowTrade, ...]) -> float:
    """Sum what following *trades* would be worth, skipping unpriced ones."""
    return sum(trade.delta_eur or 0.0 for trade in trades)


def shadow_record(
    conn: sqlite3.Connection,
    *,
    account_id: int = 1,
    today: date | None = None,
    weeks: int = RECORD_WINDOW_WEEKS,
) -> Record:
    """Build the record of what following the recommendations would have done.

    Args:
        conn: Open database connection.
        account_id: Account whose cash the keep-cash advice is measured on.
        today: Reference date, for tests.
        weeks: How far back to look.

    Returns:
        The record. It starts at the first recommendation ever made and can
        never reach further back: a recommendation the system did not make
        cannot be reconstructed without knowing what happened next.
    """
    from portfolio import fx_to_eur
    from store import latest_prices

    now = today or date.today()
    since = (now - timedelta(weeks=weeks)).isoformat()
    prices = latest_prices(conn)

    trades: list[ShadowTrade] = []
    unpriced = 0
    for row in conn.execute(
        f"""
        SELECT r.run_date, r.action, r.amount_eur, r.value_eur AS unit_eur,
               r.security_id, s.ticker
        FROM recommendation r JOIN securities s ON s.id = r.security_id
        WHERE r.action IN {_TRADE_ACTIONS!r} AND r.run_date >= ?
        ORDER BY r.run_date, r.id
        """,
        (since,),
    ):
        price_row = prices.get(int(row["security_id"]))
        rate = fx_to_eur(conn, price_row["currency"]) if price_row is not None else None
        now_eur = (
            price_row["close_native"] * rate
            if price_row is not None and rate is not None
            else None
        )
        then_eur = row["unit_eur"]
        amount = row["amount_eur"] or 0.0
        delta = percent = None
        if then_eur and now_eur is not None and amount:
            # Following a sale means being out of it, so its gain is the fall
            # avoided: the same arithmetic with the sign turned round.
            direction = 1.0 if row["action"] in _BUY_ACTIONS else -1.0
            percent = (now_eur / then_eur - 1) * 100 * direction
            delta = amount * percent / 100
        else:
            unpriced += 1
        trades.append(
            ShadowTrade(
                ticker=row["ticker"],
                action=row["action"],
                run_date=row["run_date"],
                amount_eur=amount,
                followed=_followed(conn, row),
                delta_eur=delta,
                delta_pct=percent,
            )
        )

    return Record(
        since=since,
        trades=tuple(trades),
        cash=_cash_advice(conn, account_id=account_id, since=since),
        unpriced=unpriced,
    )


def _followed(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """True when the ledger shows a matching fill soon after the advice.

    Inferred rather than asked: the owner trades at the broker and syncs, so
    the ledger is what actually happened. A fill in the same direction within
    the window counts, which will occasionally credit a trade made for an
    unrelated reason — better than a record nobody keeps up to date.
    """
    side = "BUY" if row["action"] in _BUY_ACTIONS else "SELL"
    until = (
        date.fromisoformat(row["run_date"]) + timedelta(days=RECORD_FOLLOW_WINDOW_DAYS)
    ).isoformat()
    return bool(
        conn.execute(
            """
            SELECT 1 FROM trades
            WHERE security_id = ? AND side = ? AND is_synthetic = 0
              AND trade_date BETWEEN ? AND ? LIMIT 1
            """,
            (row["security_id"], side, row["run_date"], until),
        ).fetchone()
    )


def _cash_advice(
    conn: sqlite3.Connection, *, account_id: int, since: str
) -> CashAdvice | None:
    """Measure the standing advice to keep cash against the benchmark.

    Deliberately simple: today's cash, held from the day of the most recent
    keep-cash recommendation. Money added or spent in between is not modelled,
    so this answers "what has holding this cash since then cost" rather than
    tracking every balance the account passed through.
    """
    from portfolio import cash_eur

    row = conn.execute(
        """
        SELECT run_date FROM recommendation
        WHERE action = 'KEEP_CASH' AND run_date >= ?
        ORDER BY run_date DESC, id DESC LIMIT 1
        """,
        (since,),
    ).fetchone()
    if row is None:
        return None
    cash = cash_eur(conn, account_id=account_id)
    if cash <= 0:
        return None
    move = _benchmark_move(conn, since=row["run_date"])
    return CashAdvice(
        since=row["run_date"],
        cash_eur=cash,
        benchmark_move_pct=move,
        cost_eur=cash * move / 100 if move is not None else None,
    )


def _benchmark_move(conn: sqlite3.Connection, *, since: str) -> float | None:
    """Percentage the benchmark moved from *since* to its latest stored close."""
    security = conn.execute(
        "SELECT id FROM securities WHERE ticker = ?", (BENCHMARK_TICKER,)
    ).fetchone()
    if security is None:
        return None
    then = conn.execute(
        """
        SELECT close_native FROM prices WHERE security_id = ? AND price_date <= ?
        ORDER BY price_date DESC LIMIT 1
        """,
        (security["id"], since),
    ).fetchone()
    latest = conn.execute(
        """
        SELECT close_native FROM prices WHERE security_id = ?
        ORDER BY price_date DESC LIMIT 1
        """,
        (security["id"],),
    ).fetchone()
    if then is None or latest is None or not then["close_native"]:
        return None
    return (latest["close_native"] / then["close_native"] - 1) * 100


def record_lines(record: Record) -> list[str]:
    """Render the record as plain lines, newest advice first.

    Args:
        record: The record to describe.

    Returns:
        Lines of plain text, not yet escaped. Every total carries the number of
        trades behind it, because a total without its count reads as a verdict.
    """
    lines: list[str] = []
    if record.cash is not None:
        cash = record.cash
        if cash.cost_eur is None:
            lines.append(
                f"Cash €{cash.cash_eur:,.2f} kept since {cash.since}: no benchmark "
                f"price to compare it with."
            )
        elif abs(cash.cost_eur) < 0.01:
            lines.append(
                f"Cash €{cash.cash_eur:,.2f} kept since {cash.since}: the benchmark "
                f"has barely moved since, so it has cost nothing yet."
            )
        else:
            verb = "cost" if cash.cost_eur > 0 else "saved"
            lines.append(
                f"Cash €{cash.cash_eur:,.2f} kept since {cash.since}: the benchmark "
                f"moved {cash.benchmark_move_pct:+.1f}%, so keeping it {verb} "
                f"€{abs(cash.cost_eur):,.2f}."
            )
    if not record.trades:
        lines.append(
            "No trade has been recommended yet, so there is nothing to follow."
        )
        return lines

    for trade in record.trades:
        acted = "you did it" if trade.followed else "you skipped it"
        if trade.delta_eur is None:
            outcome = "cannot be priced"
        else:
            outcome = f"{trade.delta_eur:+,.2f} ({trade.delta_pct:+.1f}%)"
        lines.append(
            f"{trade.ticker} {trade.action.lower()} €{trade.amount_eur:,.0f} on "
            f"{trade.run_date}: {acted}, {outcome}"
        )

    for label, group in (("You did", record.followed), ("You skipped", record.skipped)):
        if group:
            lines.append(f"{label} ({len(group)}): €{total_eur(group):+,.2f}")
    lines.append(
        f"All {len(record.trades)}: €{total_eur(record.trades):+,.2f} — far too few "
        f"trades to tell judgement from luck."
    )
    if record.unpriced:
        lines.append(f"{record.unpriced} could not be priced and are left out.")
    return lines
