"""Compare statement quantities with the existing trade-derived book."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_DOWN, Decimal

from config import REVOLUT_QUANTITY_TOLERANCE, REVOLUT_SEED_QUANTITY_DECIMALS
from portfolio import positions
from revolut_statement import Statement, StatementHolding
from store import correct_seed_quantities, load_trades


def _statement_holdings(
    statement: Statement,
) -> dict[tuple[str, str], StatementHolding]:
    """Key every statement holding by symbol and section currency."""
    reported = {}
    for section in statement.sections:
        for holding in section.holdings:
            key = (holding.symbol, section.currency)
            if key in reported:
                raise ValueError(
                    "Ambiguous statement holding; inspect its currency sections."
                )
            reported[key] = holding
    return reported


def reconcile(conn: sqlite3.Connection, statement: Statement) -> dict:
    """Report current-ledger differences without inventing historical positions.

    CashAsh has no ISIN on securities, so symbol/currency matches are explicitly
    provisional. Native statement balances cannot be compared with EUR book
    cash using today's FX, and synthetic opening fills are not importable history.
    """
    book = {
        (p.security.ticker, p.security.currency): p
        for p in positions(conn, account_id=1)
    }
    reported = _statement_holdings(statement)
    rows = []
    for key in sorted(book.keys() | reported.keys()):
        position, holding = book.get(key), reported.get(key)
        ledger_quantity = Decimal(str(position.quantity)) if position else Decimal(0)
        statement_quantity = holding.quantity if holding else Decimal(0)
        difference = statement_quantity - ledger_quantity
        status = (
            "only_in_statement"
            if position is None
            else "only_in_ledger"
            if holding is None
            else "quantity_match"
            if abs(difference) <= Decimal(REVOLUT_QUANTITY_TOLERANCE)
            else "quantity_difference"
        )
        rows.append(
            {
                "symbol": key[0],
                "currency": key[1],
                "isin": holding.isin if holding else None,
                "ledger_quantity": str(ledger_quantity),
                "statement_quantity": str(statement_quantity),
                "difference": str(difference),
                "status": status,
            }
        )
    return {
        "compared_at": datetime.now(UTC).isoformat(),
        "ledger_basis": "current trade-derived positions, not historical positions at the statement period end",
        "identity_basis": "symbol and currency only; ISIN identity is not verified by the ledger",
        "money_comparison": "not performed: native statement balances and unknown valuation time cannot establish EUR ledger cash or value",
        "quantity_tolerance": REVOLUT_QUANTITY_TOLERANCE,
        "rows": rows,
    }


@dataclass(frozen=True)
class SeedCorrection:
    """One synthetic opening quantity the seed screenshot truncated.

    Attributes:
        ticker: Ledger symbol.
        currency: Listing currency the statement section matched.
        trade_id: The synthetic opening BUY to correct.
        before: Quantity as stored, compared exactly when applied.
        after: The statement's full-precision quantity.
        price: Statement price in native currency.
    """

    ticker: str
    currency: str
    trade_id: int
    before: float
    after: Decimal
    price: Decimal

    @property
    def value_added(self) -> Decimal:
        """Native-currency value the truncated quantity left out."""
        return (self.after - Decimal(str(self.before))) * self.price


def seed_corrections(
    conn: sqlite3.Connection, statement: Statement
) -> tuple[list[SeedCorrection], list[dict[str, str]]]:
    """Find synthetic opening quantities a statement shows were truncated.

    The seed screenshot cuts quantities to ``REVOLUT_SEED_QUANTITY_DECIMALS``
    while its EUR value covers the whole holding, so only the quantity is wrong
    and cost basis must not move. A difference of any other shape — a missed
    trade, a rounding, a split — is refused rather than absorbed, and so is a
    security with anything beyond its one synthetic opening BUY, because its
    current position is then no longer the seed's.

    Args:
        conn: Open database connection.
        statement: Parsed statement to correct against.

    Returns:
        ``(corrections, refusals)``; each refusal names the symbol, currency and
        reason. Matching quantities appear in neither.
    """
    step = Decimal(1).scaleb(-REVOLUT_SEED_QUANTITY_DECIMALS)
    book = {
        (p.security.ticker, p.security.currency): p
        for p in positions(conn, account_id=1)
    }
    reported = _statement_holdings(statement)
    history = defaultdict(list)
    for trade in load_trades(conn, account_id=1):
        history[trade.security_id].append(trade)

    corrections: list[SeedCorrection] = []
    refusals: list[dict[str, str]] = []
    for key in sorted(book.keys() | reported.keys()):
        position, holding = book.get(key), reported.get(key)

        def refuse(reason: str, key: tuple[str, str] = key) -> None:
            refusals.append({"symbol": key[0], "currency": key[1], "reason": reason})

        if position is None:
            refuse("only in the statement; a new holding is added deliberately")
            continue
        if holding is None:
            refuse("only in the ledger; a sale is recorded as a trade")
            continue
        ledger = Decimal(str(position.quantity))
        if abs(holding.quantity - ledger) <= Decimal(REVOLUT_QUANTITY_TOLERANCE):
            continue
        trades = history[position.security.id]
        if len(trades) != 1 or not trades[0].is_synthetic or trades[0].side != "BUY":
            refuse(
                "has trades beyond its synthetic opening BUY; record the missing "
                "trade rather than correcting the seed"
            )
            continue
        opening = trades[0]
        if statement.period_end < date.fromisoformat(opening.trade_date):
            refuse("statement period ends before the seed date")
            continue
        if holding.quantity.quantize(step, rounding=ROUND_DOWN) != ledger:
            refuse(
                f"difference is not a {REVOLUT_SEED_QUANTITY_DECIMALS}-decimal "
                "truncation; investigate before correcting"
            )
            continue
        corrections.append(
            SeedCorrection(
                ticker=key[0],
                currency=key[1],
                trade_id=int(opening.id),
                before=position.quantity,
                after=holding.quantity,
                price=holding.price,
            )
        )
    return corrections, refusals


def apply_seed_corrections(
    conn: sqlite3.Connection, corrections: list[SeedCorrection], statement_sha: str
) -> None:
    """Write full-precision quantities to the opening trades and seed snapshot.

    Amounts and cash flows are untouched, so average cost per unit falls to
    what was actually paid. The seed snapshot's quantity moves too: fallback
    valuation divides its EUR value by that quantity.

    Args:
        conn: Open database connection.
        corrections: Output of :func:`seed_corrections`.
        statement_sha: SHA-256 of the archived statement, cited in each note.

    Raises:
        ValueError: If any trade changed since the corrections were computed;
            nothing is written.
    """
    correct_seed_quantities(
        conn,
        [
            (
                c.trade_id,
                c.before,
                float(c.after),
                f"Quantity corrected {c.before:g} → {c.after.normalize()} from "
                f"Revolut statement {statement_sha[:12]}: the seed screenshot "
                f"truncates to {REVOLUT_SEED_QUANTITY_DECIMALS} decimals.",
            )
            for c in corrections
        ],
    )
