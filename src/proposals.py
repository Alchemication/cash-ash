"""Writes the chat agent proposes and the owner confirms.

The agent cannot write. It builds a proposal, the owner is shown one sentence
and two buttons, and the write happens on the tap. Three reasons it is shaped
this way rather than letting a model call an INSERT:

A misparsed sentence is otherwise invisible. "Topped up to 250" and "topped up
by 250" differ by a hundred and sixty euros and read almost identically, so the
proposal states the *resulting* state — cash goes from this to that — which
makes a misreading visible before it is recorded rather than afterwards.

Validation happens when the proposal is built, not when it is applied. A
proposal that cannot be applied should never have been shown, and the owner
should not be the one to discover it.

And what is applied is the stored payload, never the conversation. The sentence
the owner agreed to and the write that follows from it are the same object.

Public API:
    Proposal          -- a validated, not-yet-applied write
    ProposalError     -- the proposal is not valid; the message says why
    cash_flow         -- build a cash-flow proposal
    trade             -- build a trade proposal for a held security
    context_note      -- build a note-append proposal
    save              -- persist a proposal, returning its id
    load              -- read one back
    confirm           -- apply it and mark it confirmed
    cancel            -- mark it cancelled without applying
    expire_stale      -- close proposals nobody answered

Example:
    from proposals import cash_flow, save

    proposal = cash_flow(conn, kind="CONTRIBUTION", new_balance_eur=250.0)
    proposal_id = save(conn, proposal)
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from config import (
    PROPOSAL_NOTE_MAX_CHARS,
    PROPOSAL_TTL_MINUTES,
)

logger = logging.getLogger(__name__)

CASH_FLOW_KINDS: dict[str, str] = {
    "CONTRIBUTION": "money you paid in",
    "WITHDRAWAL": "money you took out",
    "DIVIDEND": "a dividend that landed",
    "FEE": "a fee charged",
}
"""Cash-flow kinds a proposal may carry, and how each is described.

``OPENING_BALANCE`` is deliberately absent: it is written once by the seed and
means "everything before this point", so a second one would quietly double the
book's history.
"""

_POSITIVE_KINDS = frozenset({"CONTRIBUTION", "DIVIDEND"})
"""Kinds that add to cash. The rest are stored negative, as the ledger expects."""

NOTE_FILES: dict[str, str] = {
    "log": "your running notes",
    "watchlist": "companies you are watching but do not own",
}
"""Context files a note may be appended to.

``strategy.md`` and ``investor.md`` are not here. They constrain every
recommendation the weekly run makes, so changing one is not an append — it is an
edit that deserves to be read in full before it is agreed to. ``history.md`` is
generated.
"""


class ProposalError(ValueError):
    """Raised when a proposal cannot be built or applied, with what to do."""


@dataclass(frozen=True)
class Proposal:
    """A validated write waiting for the owner to agree to it.

    Attributes:
        kind: ``cash_flow``, ``trade`` or ``context_note``.
        payload: Exactly what applying it will use.
        summary: The one sentence the owner is asked to confirm, stating the
            resulting state so a misread is visible before it is recorded.
    """

    kind: str
    payload: dict
    summary: str


def _eur(amount: float) -> str:
    """Render a EUR amount at cent precision."""
    return f"€{amount:,.2f}"


def _validated_day(raw: str | None) -> str:
    """Return an ISO date that has already happened.

    Raises:
        ProposalError: If it is unparseable or in the future.
    """
    if not raw:
        return date.today().isoformat()
    try:
        day = date.fromisoformat(str(raw).strip())
    except ValueError as exc:
        raise ProposalError(
            f"{raw!r} is not a date. Use YYYY-MM-DD, or leave it out for today."
        ) from exc
    if day > date.today():
        raise ProposalError(
            f"{day.isoformat()} is in the future. Record money after it has "
            f"moved, not before."
        )
    return day.isoformat()


def cash_flow(
    conn: sqlite3.Connection,
    *,
    kind: str,
    amount_eur: float | None = None,
    new_balance_eur: float | None = None,
    day: str | None = None,
    note: str | None = None,
) -> Proposal:
    """Build a cash-flow proposal from a delta or from a resulting balance.

    Two ways in, because people say it both ways. "I put in 160" is a delta;
    "I topped up to 250" is an assertion about the balance, which only means
    something once the current balance is known. Taking the second as a delta is
    the expensive mistake, so it is a separate argument rather than a guess.

    Args:
        conn: Open database connection, for reading the current balance.
        kind: One of ``CASH_FLOW_KINDS``.
        amount_eur: The movement itself, as a positive number.
        new_balance_eur: The balance afterwards, when that is what was said.
        day: ISO date; today if omitted.
        note: Free text kept with the row.

    Returns:
        The proposal, whose summary states the balance before and after.

    Raises:
        ProposalError: If the arguments are unusable or mean nothing.
    """
    from portfolio import cash_eur

    kind = str(kind or "").strip().upper()
    if kind not in CASH_FLOW_KINDS:
        raise ProposalError(
            f"{kind or 'that'} is not a cash-flow kind. Use one of: "
            f"{', '.join(CASH_FLOW_KINDS)}."
        )

    if (amount_eur is None) == (new_balance_eur is None):
        raise ProposalError(
            "Give either amount_eur (how much moved) or new_balance_eur (what "
            "the balance is now), not both and not neither. If the person said "
            "'topped up to 250' that is new_balance_eur; 'topped up 250' or "
            "'added 250' is amount_eur. When it is genuinely unclear, ask them "
            "rather than choosing."
        )

    current = cash_eur(conn, account_id=1)

    if new_balance_eur is not None:
        target = _finite(new_balance_eur, "new_balance_eur")
        delta = target - current
        if abs(delta) < 0.005:
            raise ProposalError(
                f"Cash is already {_eur(current)}, so there is nothing to "
                f"record. Say so rather than proposing a zero movement."
            )
        if (delta > 0) != (kind in _POSITIVE_KINDS):
            direction = "rose" if delta > 0 else "fell"
            raise ProposalError(
                f"Cash {direction} from {_eur(current)} to {_eur(target)}, "
                f"which does not match {kind}. Check which kind it was."
            )
        signed = delta
    else:
        magnitude = abs(_finite(amount_eur, "amount_eur"))
        if magnitude < 0.005:
            raise ProposalError("A cash movement of nothing cannot be recorded.")
        signed = magnitude if kind in _POSITIVE_KINDS else -magnitude
        target = current + signed

    flow_date = _validated_day(day)
    verb = "adds" if signed > 0 else "takes"
    summary = (
        f"Record {kind} of {_eur(abs(signed))} on {flow_date}? "
        f"It {verb} {_eur(abs(signed))}, so cash goes "
        f"{_eur(current)} → {_eur(target)}."
    )
    return Proposal(
        kind="cash_flow",
        payload={
            "flow_kind": kind,
            "amount_eur": round(signed, 2),
            "flow_date": flow_date,
            "note": (note or "").strip() or None,
            "cash_before_eur": round(current, 2),
            "cash_after_eur": round(target, 2),
        },
        summary=summary,
    )


def _finite(value: object, field: str) -> float:
    """Return *value* as a finite float, or explain why it is not one."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ProposalError(f"{field} must be a number, not {value!r}.") from exc
    if not math.isfinite(number):
        raise ProposalError(f"{field} must be a real amount.")
    return number


def trade(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    side: str,
    quantity: float,
    amount_eur: float | None = None,
    price_native: float | None = None,
    fee_eur: float = 0.0,
    day: str | None = None,
    note: str | None = None,
) -> Proposal:
    """Build a trade proposal for a security already held.

    Only a security already in the book. Buying something new needs a ticker, a
    name, a listing currency, a feed symbol and a sector, and a wrong feed
    symbol produces a holding nothing can ever price — five fields a sentence
    does not carry, and a failure that shows up weeks later as a permanently
    unpriced line. That stays a deliberate act.

    The amount can arrive either way. A broker confirmation gives a EUR total;
    a person remembers a price per share. Given a price, the EUR figure is
    computed at the latest stored rate and the summary says which rate was used,
    because a trade recorded at yesterday's FX is a small error that never
    corrects itself.

    Args:
        conn: Open database connection.
        ticker: A currently held security.
        side: ``BUY`` or ``SELL``.
        quantity: Units, positive.
        amount_eur: EUR consideration excluding the fee.
        price_native: Price per unit in the security's own currency.
        fee_eur: Commission or spread charged, as a positive number.
        day: ISO date; today if omitted.
        note: Free text kept with the row.

    Returns:
        The proposal, stating the resulting position and cash.

    Raises:
        ProposalError: If the arguments are unusable or the trade impossible.
    """
    from portfolio import cash_eur, fx_to_eur, positions
    from store import load_securities

    symbol = str(ticker or "").strip().upper()
    action = str(side or "").strip().upper()
    if action not in {"BUY", "SELL"}:
        raise ProposalError(f"A trade is a BUY or a SELL, not {side!r}.")

    security = load_securities(conn).get(symbol)
    held = {
        position.security.ticker: position for position in positions(conn, account_id=1)
    }
    if security is None or symbol not in held:
        owned = ", ".join(sorted(held)) or "nothing"
        raise ProposalError(
            f"{symbol or 'That'} is not a holding, so a trade in it cannot be "
            f"recorded from chat — a security new to the book needs its "
            f"listing currency and feed symbol set deliberately, or it can "
            f"never be priced. Currently held: {owned}."
        )

    units = _finite(quantity, "quantity")
    if units <= 0:
        raise ProposalError("Quantity must be a positive number of units.")
    fee = abs(_finite(fee_eur, "fee_eur")) if fee_eur else 0.0

    position = held[symbol]
    if action == "SELL" and units > position.quantity + 1e-9:
        raise ProposalError(
            f"That sells {_units(units)} units of {symbol} but only "
            f"{_units(position.quantity)} are held. Check the quantity, or say "
            f"'all' if the position was closed."
        )

    if (amount_eur is None) == (price_native is None):
        raise ProposalError(
            "Give either amount_eur (the EUR total, excluding the fee) or "
            "price_native (the price per unit in the security's own currency), "
            "not both and not neither."
        )

    rate: float | None = None
    if price_native is not None:
        unit_price = _finite(price_native, "price_native")
        if unit_price <= 0:
            raise ProposalError("A price per unit must be positive.")
        rate = fx_to_eur(conn, security.currency)
        if rate is None:
            raise ProposalError(
                f"{symbol} is quoted in {security.currency} and no "
                f"{security.currency}/EUR rate is stored, so a price per unit "
                f"cannot be converted. Run 'main.py sync', or give the EUR "
                f"total instead."
            )
        consideration = unit_price * units * rate
    else:
        unit_price = None  # type: ignore[assignment]
        consideration = abs(_finite(amount_eur, "amount_eur"))

    if consideration < 0.005:
        raise ProposalError("A trade for nothing cannot be recorded.")

    trade_date = _validated_day(day)
    cash_before = cash_eur(conn, account_id=1)
    delta = -(consideration + fee) if action == "BUY" else consideration - fee
    cash_after = cash_before + delta
    quantity_after = (
        position.quantity + units if action == "BUY" else position.quantity - units
    )

    detail = f"{_eur(consideration)}"
    if unit_price is not None:
        detail += (
            f" ({unit_price:,.4f} {security.currency} per unit at "
            f"{rate:.4f} {security.currency}/EUR)"
        )
    if fee:
        detail += f" plus a {_eur(fee)} fee"

    summary = (
        f"Record {action} of {_units(units)} {symbol} on {trade_date} for "
        f"{detail}? Position goes {_units(position.quantity)} → "
        f"{_units(quantity_after)} units; cash goes {_eur(cash_before)} → "
        f"{_eur(cash_after)}."
    )
    if cash_after < 0:
        # Not refused: the broker executed it, so the ledger is what is wrong —
        # usually a contribution nobody recorded. Saying so is more useful than
        # blocking a trade that actually happened.
        summary += (
            " That leaves cash negative, which means a deposit is missing from "
            "the book rather than that the trade is wrong."
        )

    return Proposal(
        kind="trade",
        payload={
            "security_id": security.id,
            "ticker": symbol,
            "side": action,
            "quantity": units,
            "amount_eur": round(consideration, 2),
            "fee_eur": round(fee, 2),
            "price_native": unit_price,
            "fx_rate": rate,
            "trade_date": trade_date,
            "note": (note or "").strip() or None,
            "quantity_before": position.quantity,
            "quantity_after": quantity_after,
            "cash_after_eur": round(cash_after, 2),
        },
        summary=summary,
    )


def _units(value: float) -> str:
    """Render a unit count without inventing precision."""
    return f"{value:,.4f}".rstrip("0").rstrip(".") or "0"


def context_note(*, file: str, text: str, day: str | None = None) -> Proposal:
    """Build a proposal appending one dated line to a context file.

    One line, and one that stands on its own. These files are read back into
    later research prompts without the conversation that produced them, so an
    entry that only makes sense beside today's message is worth nothing the
    moment it is read — and an entry restating a figure the ledger already holds
    is worse than nothing, because the ledger's figure will have moved on.

    Args:
        file: ``log`` or ``watchlist``.
        text: What to record, in the owner's own words where possible.
        day: ISO date; today if omitted.

    Returns:
        The proposal, whose summary shows the line exactly as it will be written.

    Raises:
        ProposalError: If the file is not writable or the line unusable.
    """
    name = str(file or "").strip().lower().removesuffix(".md")
    if name not in NOTE_FILES:
        raise ProposalError(
            f"{file or 'that'} is not a file a note can be added to. Use one "
            f"of: {', '.join(NOTE_FILES)}. Strategy and investor notes are "
            f"edited by hand, because they constrain every recommendation."
        )

    body = " ".join(str(text or "").split())
    if not body:
        raise ProposalError("There is nothing to record.")
    if len(body) > PROPOSAL_NOTE_MAX_CHARS:
        raise ProposalError(
            f"That note is {len(body)} characters against a "
            f"{PROPOSAL_NOTE_MAX_CHARS} limit. One line that stands on its own, "
            f"not a paragraph — it is read back without this conversation."
        )

    entry_date = _validated_day(day)
    line = f"- {entry_date} {body}"
    return Proposal(
        kind="context_note",
        payload={"file": name, "line": line},
        summary=f"Add to {name}.md?\n{line}",
    )


def save(conn: sqlite3.Connection, proposal: Proposal) -> int:
    """Persist a proposal and return its id."""
    now = datetime.now()
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO pending_write
                (kind, payload_json, summary, source, proposed_at, expires_at)
            VALUES (?, ?, ?, 'chat', ?, ?)
            """,
            (
                proposal.kind,
                json.dumps(proposal.payload, ensure_ascii=False),
                proposal.summary,
                now.isoformat(timespec="seconds"),
                (now + timedelta(minutes=PROPOSAL_TTL_MINUTES)).isoformat(
                    timespec="seconds"
                ),
            ),
        )
    return int(cursor.lastrowid)


def load(conn: sqlite3.Connection, proposal_id: int) -> dict | None:
    """Return one proposal row, or None when there is no such id."""
    row = conn.execute(
        "SELECT * FROM pending_write WHERE id = ?", (proposal_id,)
    ).fetchone()
    return dict(row) if row else None


def confirm(
    conn: sqlite3.Connection, proposal_id: int, *, context_dir: Path | None = None
) -> str:
    """Apply a proposal and mark it confirmed.

    Args:
        conn: Open database connection.
        proposal_id: Which proposal.
        context_dir: Where context files live, required for a note.

    Returns:
        A sentence saying what was recorded, for the button's toast.

    Raises:
        ProposalError: If it is already resolved, expired, or unknown.
    """
    row = load(conn, proposal_id)
    if row is None:
        raise ProposalError("That proposal is gone. Ask again and I will redo it.")
    if row["resolved_at"]:
        return f"Already {row['resolution']}."
    if datetime.fromisoformat(row["expires_at"]) < datetime.now():
        _resolve(conn, proposal_id, "expired", None)
        raise ProposalError(
            "That proposal expired. Ask again so the figures are current — the "
            "balance it was built against may have moved."
        )

    payload = json.loads(row["payload_json"])
    if row["kind"] == "cash_flow":
        result = _apply_cash_flow(conn, payload)
    elif row["kind"] == "trade":
        result = _apply_trade(conn, payload)
    elif row["kind"] == "context_note":
        if context_dir is None:
            raise ProposalError("Cannot write a note without a context directory.")
        result = _apply_context_note(context_dir, payload)
    else:  # pragma: no cover - the column is constrained
        raise ProposalError(f"Unknown proposal kind {row['kind']!r}.")

    _resolve(conn, proposal_id, "confirmed", result)
    logger.info("Proposal %d confirmed: %s", proposal_id, result)
    return result


def cancel(conn: sqlite3.Connection, proposal_id: int) -> str:
    """Mark a proposal cancelled without applying it."""
    row = load(conn, proposal_id)
    if row is None:
        return "That proposal is gone."
    if row["resolved_at"]:
        return f"Already {row['resolution']}."
    _resolve(conn, proposal_id, "cancelled", None)
    return "Cancelled. Nothing was recorded."


def expire_stale(conn: sqlite3.Connection) -> int:
    """Close every unanswered proposal past its expiry.

    Returns:
        How many were closed.
    """
    with conn:
        cursor = conn.execute(
            """
            UPDATE pending_write
            SET resolved_at = ?, resolution = 'expired'
            WHERE resolved_at IS NULL AND expires_at < ?
            """,
            (
                datetime.now().isoformat(timespec="seconds"),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
    return int(cursor.rowcount or 0)


def _resolve(
    conn: sqlite3.Connection, proposal_id: int, resolution: str, result: str | None
) -> None:
    """Record how a proposal ended."""
    with conn:
        conn.execute(
            """
            UPDATE pending_write
            SET resolved_at = ?, resolution = ?, result = ?
            WHERE id = ?
            """,
            (
                datetime.now().isoformat(timespec="seconds"),
                resolution,
                result,
                proposal_id,
            ),
        )


def _apply_cash_flow(conn: sqlite3.Connection, payload: dict) -> str:
    """Insert the cash flow a proposal describes."""
    from models import CashFlow
    from portfolio import cash_eur
    from store import insert_cash_flow

    note = payload.get("note")
    row_id = insert_cash_flow(
        conn,
        CashFlow(
            flow_date=payload["flow_date"],
            kind=payload["flow_kind"],
            amount_eur=float(payload["amount_eur"]),
            note=f"{note} (via chat)" if note else "via chat",
        ),
    )
    # Read back rather than trusting the figure computed when it was proposed:
    # another write may have landed in between, and the owner should be told
    # what the balance is, not what it was expected to be.
    return (
        f"Recorded {payload['flow_kind']} of "
        f"{_eur(abs(float(payload['amount_eur'])))} "
        f"({payload['flow_date']}). Cash is now "
        f"{_eur(cash_eur(conn, account_id=1))}. [#{row_id}]"
    )


def _apply_trade(conn: sqlite3.Connection, payload: dict) -> str:
    """Insert the trade a proposal describes and report what it left behind."""
    from models import Trade
    from portfolio import cash_eur, positions
    from store import insert_trade

    note = payload.get("note")
    row_id = insert_trade(
        conn,
        Trade(
            security_id=int(payload["security_id"]),
            trade_date=payload["trade_date"],
            side=payload["side"],
            quantity=float(payload["quantity"]),
            amount_eur=float(payload["amount_eur"]),
            fee_eur=float(payload["fee_eur"]),
            price_native=payload.get("price_native"),
            fx_rate=payload.get("fx_rate"),
            note=f"{note} (via chat)" if note else "via chat",
        ),
    )
    # Read the position back rather than reporting the figure computed when it
    # was proposed: the ledger is the answer, and derived is not stored.
    now = {
        position.security.ticker: position.quantity
        for position in positions(conn, account_id=1)
    }
    remaining = now.get(payload["ticker"], 0.0)
    return (
        f"Recorded {payload['side']} of {_units(float(payload['quantity']))} "
        f"{payload['ticker']} ({payload['trade_date']}). Now holding "
        f"{_units(remaining)} units; cash is "
        f"{_eur(cash_eur(conn, account_id=1))}. [#t{row_id}]"
    )


def _apply_context_note(context_dir: Path, payload: dict) -> str:
    """Append the note a proposal describes, creating the file if needed."""
    path = context_dir / f"{payload['file']}.md"
    line = payload["line"]
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    # Written via a temporary file and renamed, so a failure halfway cannot
    # leave a half-written note where the research pipeline will read it.
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".md.tmp")
    temporary.write_text(f"{existing}{line}\n", encoding="utf-8")
    temporary.replace(path)
    return f"Added to {path.name}."
