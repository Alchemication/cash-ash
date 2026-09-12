"""Tools the chat agent may call, and their execution.

Two kinds, for two different reasons.

``run_sql`` is read-only SQL against the query views. It exists because a
conversation asks questions nobody enumerated in advance — "what did I pay for
it", "when did I last add cash", "which holdings have I never sold any of" —
and a fixed tool per question would be a worse CLI.

``portfolio_snapshot`` and ``concentration_report`` exist because SQL must not
be allowed to answer the money questions. Position cost basis is path-dependent
and a holding that cannot be priced reports None, never zero; a model writing
``SUM(quantity * price)`` would silently total an unpriced holding as nothing
and produce a plausible wrong figure. These return what ``portfolio.py``
computes, so the euro figures the agent quotes are the ones every other part of
CashAsh quotes.

What a result *says* lives in ``chat_render``; this module decides what to
fetch. Results reach the model as markdown rather than JSON, with structured
rows carried alongside the text for anything that has to compute on them.

Public API:
    chat_tools     -- tool definitions, in litellm's function-calling format
    execute_tool   -- run one tool call by name, returning text and rows
    schema_summary -- the live tables and views, for the prompt
    ToolResult     -- what a tool produced
    TOOL_NAMES     -- every name ``execute_tool`` answers to
    ChatToolError  -- a tool cannot run; the message says what to do about it

Example:
    from chat_tools import execute_tool

    result = execute_tool("run_sql", {"query": "SELECT * FROM v_trades"}, db_path)
    print(result.text)
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from config import (
    CHAT_SQL_DEFAULT_ROWS,
    CHAT_SQL_ROW_LIMIT,
    CHAT_SQL_TIMEOUT_S,
)
from chat_render import UNPRICED, eur, rows_table
from proposals import CASH_FLOW_KINDS, NOTE_FILES, ProposalError

logger = logging.getLogger(__name__)


class ChatToolError(RuntimeError):
    """A tool cannot run, and the message says what the person should do.

    Separate from an arbitrary fault so the guidance reaches the model intact:
    an unexpected exception is reported with its type, which is useful for a
    bug and noise in front of an instruction.
    """


@dataclass(frozen=True)
class ToolResult:
    """What one tool call produced.

    Attributes:
        text: Markdown for the model to read. The only thing it ever sees.
        rows: The same data structured, for code that has to compute on it —
            a chart, an eval assertion. Empty unless the tool returned a table.
        ok: False when the text is a failure the model has to react to.
        proposal: A ``proposals.Proposal`` the owner must confirm, for a tool
            that asked to change something. Returned rather than saved here so
            these tools stay read-only; the caller persists it and puts the
            buttons on it.
    """

    text: str
    rows: tuple[dict, ...] = field(default_factory=tuple)
    ok: bool = True
    proposal: object | None = None


_REQUIRED_VIEWS: frozenset[str] = frozenset(
    {"v_trades", "v_cash_flows", "v_cash_ledger", "v_cash_balance", "v_latest_price"}
)
"""Views every chat tool depends on, checked before a query is attempted."""

_READ_STATEMENT_KEYWORDS = frozenset({"SELECT", "WITH"})
"""Statement shapes a chat query may begin with.

``WITH`` is allowed because a common table expression is the natural way to
phrase a multi-step question, and refusing it leaves the model re-deriving a
query that was already correct. Nothing is granted by allowing it: read-only is
enforced by the ``mode=ro`` connection, and the ``SELECT`` wrapper below only
compiles around a statement that is already a query, so ``WITH ... INSERT``
fails twice over.
"""


def chat_tools() -> list[dict]:
    """Return every chat tool definition in litellm's function-calling format.

    Returns:
        Tool definitions, ready to pass as ``tools=``.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "run_sql",
                "description": (
                    "Run a read-only SQL query against the portfolio database "
                    "and get a markdown table back. Use it for history, "
                    "filtering and counting: trades, cash flows, prices, "
                    "recommendations, theses, research runs. Query the v_* "
                    "views rather than the tables where one exists — v_trades "
                    "carries the ticker so no id join is needed, v_cash_ledger "
                    "has every EUR movement signed, v_latest_price has the "
                    "newest close per security. Do NOT use this to compute a "
                    "position, a portfolio value, a weight or a return: call "
                    "portfolio_snapshot instead, which applies the cost-basis "
                    "and pricing rules this database's numbers depend on."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "A read-only query: SELECT, or WITH ... SELECT."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                f"Maximum rows. Default {CHAT_SQL_DEFAULT_ROWS}, "
                                f"capped at {CHAT_SQL_ROW_LIMIT}."
                            ),
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "portfolio_snapshot",
                "description": (
                    "The authoritative current state of the portfolio: every "
                    "open position with quantity, average cost, EUR value, "
                    "unrealised return, weight and how it was priced, plus cash "
                    "and the total. Any euro figure, weight or return you state "
                    "must come from here. A holding that cannot be priced is "
                    f"marked {UNPRICED} and is excluded from the total and "
                    "from every weight — say so rather than treating it as "
                    "zero."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ticker": {
                            "type": "string",
                            "description": (
                                "Restrict to one holding, which is reported in "
                                "more detail. Omit for the whole portfolio."
                            ),
                        }
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "concentration_report",
                "description": (
                    "Portfolio weights grouped by security, sector or theme, "
                    "with the configured limits and which groups breach them. "
                    "Theme weights may sum past 100% because a security carries "
                    "several themes; that is correct, not an error."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "group_by": {
                            "type": "string",
                            "enum": ["security", "sector", "theme"],
                            "description": "How to group. Defaults to security.",
                        }
                    },
                },
            },
        },
    ]


def propose_tools() -> list[dict]:
    """Return the tool definitions that ask to change something.

    Separate from :func:`chat_tools` so a caller can offer the reading tools
    without the writing ones — an eval measuring how a question is answered
    should not be able to propose a contribution.

    Returns:
        Tool definitions, ready to append to ``tools=``.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "propose_cash_flow",
                "description": (
                    "Propose recording money that moved in or out of the "
                    "account: a top-up, a withdrawal, a dividend, a fee. This "
                    "does NOT record anything — the owner is shown one sentence "
                    "and taps to confirm. Never tell them it is done; say you "
                    "have put it up for confirmation. Pass amount_eur when they "
                    "said how much moved ('added 160', 'put in 160'), or "
                    "new_balance_eur when they said what the balance now is "
                    "('topped up to 250'). Those differ by a lot and sound "
                    "alike: when it is genuinely unclear which they meant, ask "
                    "instead of choosing."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": sorted(CASH_FLOW_KINDS),
                            "description": "What kind of movement it was.",
                        },
                        "amount_eur": {
                            "type": "number",
                            "description": (
                                "How much moved, as a positive number. The sign "
                                "follows from the kind."
                            ),
                        },
                        "new_balance_eur": {
                            "type": "number",
                            "description": (
                                "What cash is now, when that is what they said "
                                "rather than how much moved."
                            ),
                        },
                        "day": {
                            "type": "string",
                            "description": (
                                "ISO date it happened. Omit for today. Never a "
                                "future date."
                            ),
                        },
                        "note": {
                            "type": "string",
                            "description": "Short reason, in their words.",
                        },
                    },
                    "required": ["kind"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "propose_trade",
                "description": (
                    "Propose recording a buy or a sell the owner already made "
                    "at their broker. This does NOT record anything — they "
                    "confirm with a button, and you must not say it is done. "
                    "Only for a security already in the portfolio: something "
                    "new needs its listing currency and feed symbol set "
                    "deliberately or it can never be priced, so say that and "
                    "do not attempt it. Give amount_eur when they said the "
                    "total they paid or received, or price_native when they "
                    "said a price per share — 'bought 2 at 470' is "
                    "price_native. Quantity is units, always positive; the "
                    "side carries the direction. This changes every derived "
                    "figure in the book, so if the quantity, the price or "
                    "which way round it was is unclear, ask."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ticker": {
                            "type": "string",
                            "description": "A currently held security.",
                        },
                        "side": {
                            "type": "string",
                            "enum": ["BUY", "SELL"],
                            "description": "Which way the trade went.",
                        },
                        "quantity": {
                            "type": "number",
                            "description": "Units traded, positive.",
                        },
                        "amount_eur": {
                            "type": "number",
                            "description": (
                                "EUR total, excluding the fee, when that is "
                                "what they gave."
                            ),
                        },
                        "price_native": {
                            "type": "number",
                            "description": (
                                "Price per unit in the security's own "
                                "currency, when that is what they gave."
                            ),
                        },
                        "fee_eur": {
                            "type": "number",
                            "description": "Commission charged, positive.",
                        },
                        "day": {
                            "type": "string",
                            "description": "ISO date it filled. Omit for today.",
                        },
                        "note": {
                            "type": "string",
                            "description": "Short reason, in their words.",
                        },
                    },
                    "required": ["ticker", "side", "quantity"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "propose_context_note",
                "description": (
                    "Propose appending one dated line to the owner's own notes: "
                    "'log' for why they did something, what they are watching, "
                    "a doubt they have not acted on; 'watchlist' for a company "
                    "they are interested in but do not own. This does NOT write "
                    "anything — they confirm with a button. Only when they are "
                    "telling you something to keep. A question is not a note, "
                    "and neither is something you worked out yourself. These "
                    "lines are read back into later research without this "
                    "conversation, so the line must make sense alone, and must "
                    "not restate a value, weight or return the ledger already "
                    "holds — those move, the note does not."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file": {
                            "type": "string",
                            "enum": sorted(NOTE_FILES),
                            "description": "Which of their notes to add to.",
                        },
                        "text": {
                            "type": "string",
                            "description": (
                                "One line, their words where possible, their "
                                "numbers verbatim. No date — it is added."
                            ),
                        },
                        "day": {
                            "type": "string",
                            "description": "ISO date it concerns. Omit for today.",
                        },
                    },
                    "required": ["file", "text"],
                },
            },
        },
    ]


TOOL_NAMES: frozenset[str] = frozenset(
    tool["function"]["name"] for tool in (*chat_tools(), *propose_tools())
)


def execute_tool(name: str, arguments: dict, db_path: Path) -> ToolResult:
    """Run one tool call and return what the model should read.

    Never raises: a tool failure is a result the model has to read and react
    to, and an exception here would end the conversation instead of letting it
    try a different question.

    Args:
        name: Tool function name.
        arguments: Parsed JSON arguments.
        db_path: Path to the profile's database.

    Returns:
        The rendered result, or a failure whose ``ok`` is False.
    """
    if not isinstance(arguments, dict):
        arguments = {}
    try:
        if name == "run_sql":
            return _run_sql(db_path, arguments)
        if name == "portfolio_snapshot":
            return _portfolio_snapshot(db_path, arguments)
        if name == "concentration_report":
            return _concentration_report(db_path, arguments)
        if name == "propose_cash_flow":
            return _propose_cash_flow(db_path, arguments)
        if name == "propose_trade":
            return _propose_trade(db_path, arguments)
        if name == "propose_context_note":
            return _propose_context_note(arguments)
    except ProposalError as exc:
        # Not a fault: the model asked for something the rules refuse, and the
        # message says what would be acceptable instead.
        logger.info("Tool %s refused: %s", name, exc)
        return _failed(str(exc))
    except ChatToolError as exc:
        logger.warning("Tool %s cannot run: %s", name, exc)
        return _failed(str(exc))
    except Exception as exc:  # noqa: BLE001 - a tool fault must not end the chat
        logger.warning("Tool %s failed: %s", name, exc, exc_info=True)
        return _failed(f"{type(exc).__name__}: {exc}")
    return _failed(f"No tool named {name}. Available: {', '.join(sorted(TOOL_NAMES))}.")


def _failed(message: str) -> ToolResult:
    """Render a failure the model has to read and work around."""
    return ToolResult(text=f"Error: {message}", ok=False)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _open_read_only(db_path: Path) -> sqlite3.Connection:
    """Open the database read-only, without migrating it.

    One of the few legitimate reasons to bypass ``open_db``: this connection
    must not be able to write, and ``mode=ro`` also cannot create a database,
    so it cannot be the thing that brings an empty one into existence.

    That also means it cannot apply a pending migration, so a database older
    than the query views fails here. The views are checked for by name and the
    failure says which command fixes it, because SQLite's own "no such table:
    v_cash_balance" reads like a bug in CashAsh rather than a database that has
    not been migrated yet.

    Raises:
        ChatToolError: If the file is unreadable or predates the query views.
    """
    uri = f"file:{db_path.resolve()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=CHAT_SQL_TIMEOUT_S)
        conn.row_factory = sqlite3.Row
    except sqlite3.OperationalError as exc:
        raise ChatToolError(
            f"Cannot read {db_path}: {exc}. If the database does not exist yet, "
            f"create it with 'uv run python main.py init'."
        ) from exc

    present = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")
    }
    missing = sorted(_REQUIRED_VIEWS - present)
    if missing:
        conn.close()
        raise ChatToolError(
            f"This database is missing the query views ({', '.join(missing)}). "
            f"Apply pending migrations with 'uv run python main.py db migrate', "
            f"then ask again."
        )
    return conn


def schema_summary(db_path: Path) -> str:
    """Return every queryable table and view with its columns.

    Read from the live database rather than written down in the prompt, because
    a hand-maintained schema reference drifts the first time a migration lands
    and then costs a round-trip per wrong column name. Views come first: they
    are what a query should normally use.

    Args:
        db_path: Path to the profile's database.

    Returns:
        One line per relation, compact enough to sit in every prompt.

    Raises:
        ChatToolError: If the database cannot be read.
    """
    conn = _open_read_only(db_path)
    try:
        relations = conn.execute(
            """
            SELECT name, type FROM sqlite_master
            WHERE type IN ('table', 'view')
              AND name NOT LIKE 'sqlite_%'
              AND name <> 'schema_migrations'
            ORDER BY type DESC, name
            """
        ).fetchall()
        lines: list[str] = []
        for relation in relations:
            columns = [
                str(column["name"])
                for column in conn.execute(f"PRAGMA table_info({relation['name']})")
            ]
            lines.append(f"{relation['name']}({', '.join(columns)})")
    finally:
        conn.close()
    return "\n".join(lines)


def _run_sql(db_path: Path, arguments: dict) -> ToolResult:
    """Execute a read-only query and render the rows as a table."""
    query = str(arguments.get("query") or "").strip()
    if not query:
        return _failed("Empty query.")

    first = query.lstrip("( \t\n").split()[0].upper() if query.split() else ""
    if first not in _READ_STATEMENT_KEYWORDS:
        return _failed("Only SELECT and WITH (read-only) queries are allowed.")

    # A trailing semicolon is idiomatic SQL and every model writes one
    # eventually, but it cannot survive being wrapped in a subquery: the result
    # is "near ';': syntax error", which reads like a fault in the query itself
    # and costs a round-trip re-deriving something already correct.
    query = query.rstrip().rstrip(";").rstrip()
    if not query:
        return _failed("Empty query.")

    try:
        requested = int(arguments.get("limit", CHAT_SQL_DEFAULT_ROWS))
    except (TypeError, ValueError):
        requested = CHAT_SQL_DEFAULT_ROWS
    limit = max(1, min(requested, CHAT_SQL_ROW_LIMIT))
    wrapped = f"SELECT * FROM ({query}) LIMIT {limit}"

    # Run on a thread so a pathological query is abandoned rather than leaving
    # the person watching a placeholder forever. SQLite keeps working in the
    # background; the daemon simply stops waiting for it.
    outcome: list[ToolResult] = [
        _failed(
            f"Query timed out after {CHAT_SQL_TIMEOUT_S:g}s. Narrow it, or "
            f"aggregate instead of listing rows."
        )
    ]

    def _run() -> None:
        try:
            conn = _open_read_only(db_path)
            try:
                cursor = conn.execute(wrapped)
                columns = [column[0] for column in cursor.description or []]
                rows = [dict(row) for row in cursor.fetchall()]
            finally:
                conn.close()
            outcome[0] = ToolResult(
                text=rows_table(columns, rows, limit), rows=tuple(rows)
            )
        except ChatToolError as exc:
            logger.warning("Chat query cannot run: %s", exc)
            outcome[0] = _failed(str(exc))
        except Exception as exc:  # noqa: BLE001 - the model reads the message
            logger.warning("Chat query failed: %s", exc)
            outcome[0] = _failed(str(exc))

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=CHAT_SQL_TIMEOUT_S + 1)
    return outcome[0]


def _portfolio_snapshot(db_path: Path, arguments: dict) -> ToolResult:
    """Render the canonical portfolio state, with unpriced holdings named."""
    from chat_render import holding_row, holdings_table, one_holding
    from portfolio import cash_eur, holdings, total_value
    from quality import valuation_gaps

    ticker = str(arguments.get("ticker") or "").strip().upper()

    conn = _open_read_only(db_path)
    try:
        rows = holdings(conn, account_id=1)
        cash = cash_eur(conn, account_id=1)
        gaps = valuation_gaps(conn, rows, date.today())
    finally:
        conn.close()

    if not rows:
        return ToolResult(
            text=(
                f"No open positions. Cash is {eur(cash)}. Nothing has been "
                f"bought, or everything has been sold."
            )
        )

    # The total covers every holding even when one ticker was asked for,
    # because a weight means nothing against a filtered total.
    total = total_value(rows, cash=cash)

    if ticker:
        selected = [row for row in rows if row.position.security.ticker == ticker]
        if not selected:
            held = ", ".join(row.position.security.ticker for row in rows)
            return _failed(f"No open position in {ticker}. Currently held: {held}.")
        return ToolResult(
            text=one_holding(selected[0], total=total, cash=cash, gaps=gaps),
            rows=(holding_row(selected[0], total),),
        )

    return ToolResult(
        text=holdings_table(
            rows,
            total=total,
            cash=cash,
            gaps=gaps,
            as_of=date.today().isoformat(),
        ),
        rows=tuple(holding_row(row, total) for row in rows),
    )


def _concentration_report(db_path: Path, arguments: dict) -> ToolResult:
    """Render grouped weights with the configured limits alongside."""
    from chat_render import concentration_table
    from config import CONCENTRATION_ALERT_PCT, MAX_POSITION_WEIGHT_PCT
    from portfolio import cash_eur, concentration, holdings, total_value

    group_by = str(arguments.get("group_by") or "security").strip().lower()
    if group_by not in {"security", "sector", "theme"}:
        return _failed("group_by must be one of: security, sector, theme.")

    conn = _open_read_only(db_path)
    try:
        rows = holdings(conn, account_id=1)
        cash = cash_eur(conn, account_id=1)
    finally:
        conn.close()

    total = total_value(rows, cash=cash)
    groups = concentration(rows, key=group_by, total=total)
    if not groups:
        return ToolResult(
            text=f"Nothing to group by {group_by}: no holding can be valued."
        )

    return ToolResult(
        text=concentration_table(
            groups,
            group_by=group_by,
            total=total,
            cash=cash,
            unpriced=[
                row.position.security.ticker for row in rows if row.value_eur is None
            ],
            max_weight_pct=MAX_POSITION_WEIGHT_PCT,
            alert_pct=CONCENTRATION_ALERT_PCT,
        ),
        rows=tuple(
            {
                "label": group.label,
                "value_eur": group.value_eur,
                "weight_pct": group.weight_pct,
                "over_limit": group.over_limit,
            }
            for group in groups
        ),
    )


def _propose_cash_flow(db_path: Path, arguments: dict) -> ToolResult:
    """Build a cash-flow proposal for the owner to confirm.

    Reads the current balance on the read-only connection and writes nothing:
    the proposal travels back to the caller, which is what persists it.
    """
    from proposals import cash_flow

    conn = _open_read_only(db_path)
    try:
        proposal = cash_flow(
            conn,
            kind=str(arguments.get("kind") or ""),
            amount_eur=arguments.get("amount_eur"),
            new_balance_eur=arguments.get("new_balance_eur"),
            day=arguments.get("day"),
            note=arguments.get("note"),
        )
    finally:
        conn.close()
    return _proposed(proposal)


def _propose_context_note(arguments: dict) -> ToolResult:
    """Build a note-append proposal for the owner to confirm."""
    from proposals import context_note

    proposal = context_note(
        file=str(arguments.get("file") or ""),
        text=str(arguments.get("text") or ""),
        day=arguments.get("day"),
    )
    return _proposed(proposal)


def _proposed(proposal) -> ToolResult:  # type: ignore[no-untyped-def]
    """Render a built proposal for the model.

    The wording is firm about what has *not* happened. A model told only
    "proposed" will cheerfully report back that the contribution is recorded,
    and the owner then stops looking for the button.
    """
    return ToolResult(
        text=(
            f"Put up for confirmation — nothing is recorded yet. The owner will "
            f"see this and two buttons:\n\n{proposal.summary}\n\n"
            f"Tell them you have put it up to confirm. Do not say it is done, "
            f"and do not ask them to confirm in words: the buttons do that."
        ),
        proposal=proposal,
    )


def _propose_trade(db_path: Path, arguments: dict) -> ToolResult:
    """Build a trade proposal for the owner to confirm."""
    from proposals import trade

    conn = _open_read_only(db_path)
    try:
        proposal = trade(
            conn,
            ticker=str(arguments.get("ticker") or ""),
            side=str(arguments.get("side") or ""),
            quantity=arguments.get("quantity"),
            amount_eur=arguments.get("amount_eur"),
            price_native=arguments.get("price_native"),
            fee_eur=arguments.get("fee_eur") or 0.0,
            day=arguments.get("day"),
            note=arguments.get("note"),
        )
    finally:
        conn.close()
    return _proposed(proposal)
