"""Turning statement activity rows into ledger entries.

The statement is the record of what actually happened at the broker, so a fill
in it is the trade — not an intention, not a recommendation someone approved.
Importing it is what makes "I traded, then I synced" the whole workflow.

Three rules keep the import from inventing history:

- **Only rows it recognises.** A row whose shape is not understood is reported,
  never guessed at. A misparsed fill is a wrong position that looks right.
- **Only after the opening snapshot.** The seed already represents everything
  held on its date as synthetic opening trades; importing a fill from before it
  would count the same shares twice.
- **Only once.** Statements overlap, and re-importing a month is the normal way
  to recover a failed download, so every row carries a reference derived from
  its own contents and is inserted at most once.

Money is stored in EUR, and the statement states native amounts, so a fill
needs the rate on its own trade date. Where that rate cannot be established the
fill is skipped and named, rather than converted at today's rate — which would
misstate the cost basis of every position it touched.

Public API:
    Fill            -- one buy or sell parsed from a statement
    Payout          -- one dividend parsed from a statement
    ImportResult    -- what an import added, skipped and had already
    parse_activity  -- read fills and payouts out of a statement
    import_activity -- write them to the ledger

Example:
    from revolut_fills import import_activity

    result = import_activity(conn, statement)
    print(result.summary())
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from revolut_statement import Statement, StatementTransaction

logger = logging.getLogger(__name__)

_NUMBER = r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_MONEY = rf"(?:US\$|€|£|USD\s*|EUR\s*|GBP\s*)(?P<price>{_NUMBER})"

_TRADE_ROW = re.compile(
    rf"^(?P<symbol>\S+)\s+(?P<kind>Market|Limit|Stop)\s+(?P<quantity>{_NUMBER})\s+"
    rf"{_MONEY}\s+(?P<side>Buy|Sell)$"
)
"""A filled order: symbol, order type, quantity, price, side."""

_DIVIDEND_ROW = re.compile(r"^(?P<symbol>\S+)\s+Dividend$")

_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


@dataclass(frozen=True)
class Fill:
    """One buy or sell, in the currency its statement section reports.

    Attributes:
        symbol: Broker symbol.
        currency: Section currency; the price and value are in it.
        side: BUY or SELL, as the ledger records them.
        traded_on: Date of the fill.
        quantity: Units filled.
        price_native: Price per unit.
        value_native: Consideration before fees.
        fees_native: Fees and commission together.
        source_ref: Identity derived from the row, so it imports once.
    """

    symbol: str
    currency: str
    side: str
    traded_on: date
    quantity: Decimal
    price_native: Decimal
    value_native: Decimal
    fees_native: Decimal
    source_ref: str


@dataclass(frozen=True)
class Payout:
    """One dividend, in the currency its statement section reports."""

    symbol: str
    currency: str
    paid_on: date
    amount_native: Decimal
    source_ref: str


@dataclass
class ImportResult:
    """What one import did.

    Attributes:
        trades: Fills written to the ledger.
        dividends: Dividends written as cash flows.
        already: Rows already imported by an earlier statement.
        skipped: One line per row left out, saying why.
    """

    trades: int = 0
    dividends: int = 0
    already: int = 0
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """One line for a report or a Telegram message."""
        parts = [f"{self.trades} trade(s)", f"{self.dividends} dividend(s)"]
        if self.already:
            parts.append(f"{self.already} already recorded")
        if self.skipped:
            parts.append(f"{len(self.skipped)} not imported")
        return "Imported " + ", ".join(parts) + "."


def _day(timestamp: str) -> date:
    """Read the date out of a statement timestamp, ignoring its clock time."""
    day, month, year = timestamp.split()[:3]
    return date(int(year), _MONTHS.index(month) + 1, int(day))


def parse_activity(
    statement: Statement,
) -> tuple[list[Fill], list[Payout], list[str]]:
    """Read fills and dividends out of a statement.

    Args:
        statement: A parsed statement.

    Returns:
        ``(fills, payouts, unrecognised)``, where the third is one line per row
        whose shape is not understood. Those are reported rather than guessed
        at: a misparsed fill is a wrong position that looks right.
    """
    fills: list[Fill] = []
    payouts: list[Payout] = []
    unrecognised: list[str] = []
    for section in statement.sections:
        for row in section.transactions:
            fill = _as_fill(row, section.currency)
            if fill is not None:
                fills.append(fill)
                continue
            payout = _as_payout(row, section.currency)
            if payout is not None:
                payouts.append(payout)
                continue
            unrecognised.append(f"{row.timestamp}: {row.details} — row not recognised")
    return fills, payouts, unrecognised


def _as_fill(row: StatementTransaction, currency: str) -> Fill | None:
    """Parse one activity row as a fill, or return None."""
    match = _TRADE_ROW.match(row.details)
    if match is None:
        return None
    quantity = Decimal(match.group("quantity").replace(",", ""))
    price = Decimal(match.group("price").replace(",", ""))
    side = "BUY" if match.group("side") == "Buy" else "SELL"
    return Fill(
        symbol=match.group("symbol"),
        currency=currency,
        side=side,
        traded_on=_day(row.timestamp),
        quantity=quantity,
        price_native=price,
        value_native=row.value,
        fees_native=row.fees + row.commission,
        source_ref=_reference(row, currency),
    )


def _as_payout(row: StatementTransaction, currency: str) -> Payout | None:
    """Parse one activity row as a dividend, or return None."""
    match = _DIVIDEND_ROW.match(row.details)
    if match is None:
        return None
    return Payout(
        symbol=match.group("symbol"),
        currency=currency,
        paid_on=_day(row.timestamp),
        amount_native=row.value - row.fees - row.commission,
        source_ref=_reference(row, currency),
    )


def _reference(row: StatementTransaction, currency: str) -> str:
    """Identity for one statement row, stable across overlapping statements."""
    return f"revolut:{currency}:{row.timestamp}:{row.details}:{row.value}"


def import_activity(
    conn: sqlite3.Connection,
    statement: Statement,
    *,
    account_id: int = 1,
    provider=None,  # type: ignore[no-untyped-def]
) -> ImportResult:
    """Write a statement's fills and dividends to the ledger.

    Args:
        conn: Open database connection.
        statement: A parsed statement.
        account_id: Account the rows belong to.
        provider: Market data provider used to fetch a missing FX rate; the
            configured one by default.

    Returns:
        What was added, what was already there, and what was left out.
    """
    from models import CashFlow, Trade
    from store import insert_cash_flow, insert_trade, load_securities

    fills, payouts, unrecognised = parse_activity(statement)
    result = ImportResult(skipped=list(unrecognised))
    securities = load_securities(conn)
    opening = conn.execute(
        "SELECT MAX(trade_date) FROM trades WHERE is_synthetic = 1"
    ).fetchone()[0]

    for fill in fills:
        security = securities.get(fill.symbol.upper())
        if security is None or security.id is None:
            result.skipped.append(f"{fill.symbol}: not a known holding")
            continue
        if opening and fill.traded_on.isoformat() <= opening:
            result.skipped.append(
                f"{fill.symbol} {fill.traded_on}: on or before the opening "
                f"snapshot, already represented by it"
            )
            continue
        if _already(conn, "trades", fill.source_ref):
            result.already += 1
            continue
        rate = _rate(conn, fill.currency, fill.traded_on, provider)
        if rate is None:
            result.skipped.append(
                f"{fill.symbol} {fill.traded_on}: no {fill.currency} rate for that "
                f"day, so its EUR cost cannot be established"
            )
            continue
        insert_trade(
            conn,
            Trade(
                security_id=security.id,
                trade_date=fill.traded_on.isoformat(),
                side=fill.side,
                quantity=float(fill.quantity),
                amount_eur=float(fill.value_native) * rate,
                account_id=account_id,
                price_native=float(fill.price_native),
                fx_rate=rate,
                fee_eur=float(fill.fees_native) * rate,
                note="Revolut statement",
                source_ref=fill.source_ref,
            ),
        )
        result.trades += 1

    for payout in payouts:
        if opening and payout.paid_on.isoformat() <= opening:
            result.skipped.append(
                f"{payout.symbol} {payout.paid_on}: paid on or before the opening "
                f"snapshot"
            )
            continue
        if _already(conn, "cash_flows", payout.source_ref):
            result.already += 1
            continue
        rate = _rate(conn, payout.currency, payout.paid_on, provider)
        if rate is None:
            result.skipped.append(
                f"{payout.symbol} {payout.paid_on}: no {payout.currency} rate for "
                f"that day, so its EUR amount cannot be established"
            )
            continue
        insert_cash_flow(
            conn,
            CashFlow(
                flow_date=payout.paid_on.isoformat(),
                kind="DIVIDEND",
                amount_eur=float(payout.amount_native) * rate,
                account_id=account_id,
                note=f"{payout.symbol} dividend, Revolut statement",
                source_ref=payout.source_ref,
            ),
        )
        result.dividends += 1

    return result


def _already(conn: sqlite3.Connection, table: str, source_ref: str) -> bool:
    """True when this statement row has already been imported."""
    return bool(
        conn.execute(
            f"SELECT 1 FROM {table} WHERE source_ref = ? LIMIT 1",  # noqa: S608
            (source_ref,),
        ).fetchone()
    )


def _rate(
    conn: sqlite3.Connection,
    currency: str,
    day: date,
    provider,  # type: ignore[no-untyped-def]
) -> float | None:
    """Return the EUR rate for *currency* on *day*, fetching it if need be.

    A trade's cost basis is struck at the rate of its own day, and today's rate
    cannot stand in: it would restate what a position cost every time the euro
    moved. The rate is stored once fetched, so a reimport needs no network.
    """
    from store import save_fx_rate

    if currency == "EUR":
        return 1.0
    stored = conn.execute(
        """
        SELECT rate FROM fx_rates WHERE base = ? AND quote = 'EUR' AND rate_date <= ?
        ORDER BY rate_date DESC LIMIT 1
        """,
        (currency, day.isoformat()),
    ).fetchone()
    if stored is not None and _fresh(conn, currency, day):
        return float(stored["rate"])

    from market_data import ProviderError, get_provider

    try:
        history = (provider or get_provider()).fetch_history(
            f"{currency}EUR=X", start=day.isoformat()
        )
    except (ProviderError, ValueError) as exc:  # noqa: BLE001 - reported, not raised
        logger.warning("Could not fetch %s history: %s", currency, exc)
        history = {}
    for when, close in sorted(history.items()):
        save_fx_rate(
            conn,
            rate_date=when,
            base=currency,
            quote="EUR",
            rate=close,
            source="statement import",
        )
    on_day = [
        close for when, close in sorted(history.items()) if when <= day.isoformat()
    ]
    if on_day:
        return float(on_day[-1])
    return float(stored["rate"]) if stored is not None else None


def _fresh(conn: sqlite3.Connection, currency: str, day: date) -> bool:
    """True when a stored rate sits on the day itself, not merely before it."""
    return bool(
        conn.execute(
            "SELECT 1 FROM fx_rates WHERE base = ? AND quote = 'EUR' AND rate_date = ?",
            (currency, day.isoformat()),
        ).fetchone()
    )
