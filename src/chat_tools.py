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

Results are rendered as markdown, not JSON. JSON spends tokens repeating a key
on every row and leaves the reasoning implicit: ``"value_eur": null`` beside
``"unrealised_return_pct": null`` has to be interpreted, where "UNPRICED —
excluded from the total" states it. Structured rows are still carried alongside
the text for anything that needs to compute on them, such as drawing a chart.

One inversion worth noting against the same approach in zdrowskit, where a
missing metric is simply left out of the rendering: here a missing price is
stated as loudly as a present one. An omitted holding would read as a portfolio
that does not contain it.

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
    """

    text: str
    rows: tuple[dict, ...] = field(default_factory=tuple)
    ok: bool = True


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

_MAX_CELL_CHARS = 300
"""Longest value rendered in one table cell before it is cut short.

The row cap bounds how many rows a query returns, not how large one is, and
``llm_call.messages_json`` holds a whole prompt in a single cell — one row of
it would crowd out the conversation it is part of. Three hundred characters is
enough to see what a value is and decide whether to ask about it specifically.
"""

_UNPRICED = "UNPRICED"
"""How a holding with no price is shown.

A word rather than a blank or a dash, because it has to survive being read
quickly: the one mistake that matters here is treating it as zero.
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
                    f"marked {_UNPRICED} and is excluded from the total and "
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


TOOL_NAMES: frozenset[str] = frozenset(
    tool["function"]["name"] for tool in chat_tools()
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
# Rendering
# ---------------------------------------------------------------------------


def _eur(amount: float) -> str:
    """Render a EUR amount at cent precision, thousands separated."""
    return f"€{amount:,.2f}"


def _pct(value: float, *, signed: bool = False) -> str:
    """Render a percentage at one decimal, optionally with an explicit sign."""
    return f"{value:+.1f}%" if signed else f"{value:.1f}%"


def _quantity(value: float) -> str:
    """Render a share count without inventing precision it does not have.

    Fractional-share brokers produce quantities like 0.0741, and 2.0 shares is
    two shares. Trailing zeros are dropped so neither reads as noise.
    """
    text = f"{value:,.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _cell(value: object) -> str:
    """Render one arbitrary SQL value for a markdown table.

    SQL NULL is spelled out rather than blanked: a query about what is missing
    is a normal question, and an empty cell would read as a rendering gap. A
    pipe or newline inside a value is neutralised so one trade note cannot
    break the table it sits in.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        # Six significant figures keeps a price readable and stops a float
        # artefact like 50.333333333333336 from looking like precision.
        text = f"{value:.6g}"
    elif isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    text = text.replace("|", "\\|").replace("\n", " ").strip()
    if len(text) > _MAX_CELL_CHARS:
        return f"{text[:_MAX_CELL_CHARS]}… [{len(text)} chars, truncated]"
    return text


def _table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a markdown pipe table, header once and values after."""
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


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
            outcome[0] = _render_rows(columns, rows, limit)
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


def _render_rows(columns: list[str], rows: list[dict], limit: int) -> ToolResult:
    """Render query rows as a table, saying plainly when there are none."""
    if not rows:
        return ToolResult(
            text="No rows. The query ran and matched nothing.", rows=(), ok=True
        )

    body = _table(
        columns, [[_cell(row.get(column)) for column in columns] for row in rows]
    )
    header = f"{len(rows)} row{'s' if len(rows) != 1 else ''}."
    if len(rows) >= limit:
        # The model cannot tell a complete answer from a truncated one, and a
        # truncated one silently becomes "that is all of them".
        header += (
            f" This is the {limit}-row limit, so there may be more. "
            f"Aggregate, or narrow the query, before concluding anything about "
            f"totals or counts."
        )
    return ToolResult(text=f"{header}\n\n{body}", rows=tuple(rows), ok=True)


def _portfolio_snapshot(db_path: Path, arguments: dict) -> ToolResult:
    """Render the canonical portfolio state, with unpriced holdings named."""
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
                f"No open positions. Cash is {_eur(cash)}. Nothing has been "
                f"bought, or everything has been sold."
            )
        )

    # The total covers every holding even when one ticker was asked for,
    # because a weight means nothing against a filtered total.
    total = total_value(rows, cash=cash)
    unpriced = [row.position.security.ticker for row in rows if row.value_eur is None]

    if ticker:
        selected = [row for row in rows if row.position.security.ticker == ticker]
        if not selected:
            held = ", ".join(row.position.security.ticker for row in rows)
            return _failed(f"No open position in {ticker}. Currently held: {held}.")
        return ToolResult(
            text=_render_one_holding(selected[0], total=total, cash=cash, gaps=gaps),
            rows=(_holding_row(selected[0], total),),
        )

    table = _table(
        [
            "Ticker",
            "Qty",
            "Avg cost",
            "Cost basis",
            "Value",
            "Return",
            "Weight",
            "Priced",
        ],
        [_holding_cells(row, total) for row in rows],
    )
    lines = [
        f"Portfolio as of {date.today().isoformat()} — "
        f"{len(rows)} open position{'s' if len(rows) != 1 else ''}.",
        "",
        table,
        "",
        f"Cash: {_eur(cash)}.",
        f"Total: {_eur(total)} — cash plus "
        f"{len(rows) - len(unpriced)} priced holding"
        f"{'s' if len(rows) - len(unpriced) != 1 else ''}.",
    ]
    lines.extend(_unpriced_warning(unpriced, gaps))
    return ToolResult(
        text="\n".join(lines), rows=tuple(_holding_row(row, total) for row in rows)
    )


def _unpriced_warning(unpriced: list[str], gaps: list[str]) -> list[str]:
    """State what could not be priced, and what that does to the total."""
    if not unpriced:
        return []
    return [
        "",
        f"{_UNPRICED}: {', '.join(unpriced)}. Excluded from the total and from "
        f"every weight above, so the total is a floor and not what the "
        f"portfolio is worth. Never treat an unpriced holding as zero — say it "
        f"cannot be valued.",
        f"Why: {'; '.join(gaps)}." if gaps else "",
    ]


def _weight_pct(holding, total: float) -> float | None:  # type: ignore[no-untyped-def]
    """Return a holding's weight, or None when it cannot be valued."""
    if holding.value_eur is None or total <= 0:
        return None
    return holding.value_eur / total * 100.0


def _priced_label(holding) -> str:  # type: ignore[no-untyped-def]
    """Say when and from where a holding was priced."""
    if holding.value_eur is None:
        return "never"
    date_part = holding.price_date or "undated"
    return (
        f"{date_part} ({holding.price_source})" if holding.price_source else date_part
    )


def _holding_cells(holding, total: float) -> list[str]:  # type: ignore[no-untyped-def]
    """Render one holding as a table row."""
    position = holding.position
    weight = _weight_pct(holding, total)
    avg = position.avg_cost_eur
    return [
        position.security.ticker,
        _quantity(position.quantity),
        _eur(avg) if avg is not None else "—",
        _eur(position.cost_basis_eur),
        _eur(holding.value_eur) if holding.value_eur is not None else _UNPRICED,
        (
            _pct(holding.unrealised_return_pct, signed=True)
            if holding.unrealised_return_pct is not None
            else "—"
        ),
        _pct(weight) if weight is not None else "—",
        _priced_label(holding),
    ]


def _holding_row(holding, total: float) -> dict:  # type: ignore[no-untyped-def]
    """Return one holding as structured data, for charts and assertions."""
    position = holding.position
    return {
        "ticker": position.security.ticker,
        "name": position.security.name,
        "sector": position.security.sector,
        "quantity": position.quantity,
        "cost_basis_eur": position.cost_basis_eur,
        "value_eur": holding.value_eur,
        "unrealised_return_pct": holding.unrealised_return_pct,
        "weight_pct": _weight_pct(holding, total),
    }


def _render_one_holding(  # type: ignore[no-untyped-def]
    holding, *, total: float, cash: float, gaps: list[str]
) -> str:
    """Render a single holding in full.

    Spelled out rather than tabulated: there is no token pressure for one row,
    and the fields a table leaves out — sector, native currency, how it is
    priced, what has already been realised — are the ones a follow-up question
    tends to be about.
    """
    position = holding.position
    security = position.security
    weight = _weight_pct(holding, total)
    avg = position.avg_cost_eur

    lines = [
        f"{security.ticker} — {security.name}",
        f"- Sector: {security.sector or 'unclassified'}; "
        f"{security.asset_class}; quoted in {security.currency}; "
        f"priced by {security.pricing_mode}.",
        f"- Holding: {_quantity(position.quantity)} units at an average "
        f"{_eur(avg) if avg is not None else 'unknown'} each, "
        f"{_eur(position.cost_basis_eur)} invested including fees.",
    ]
    if holding.value_eur is None:
        lines.append(
            f"- Value: {_UNPRICED}. Nothing can price it right now, so it is "
            f"excluded from the {_eur(total)} portfolio total and has no "
            f"weight. Do not report it as zero."
        )
    else:
        lines.append(
            f"- Value: {_eur(holding.value_eur)}, "
            f"{_pct(holding.unrealised_return_pct or 0.0, signed=True)} "
            f"unrealised ({_eur(holding.unrealised_pnl_eur or 0.0)}), "
            f"{_pct(weight or 0.0)} of the portfolio."
        )
        lines.append(f"- Priced: {_priced_label(holding)}.")
    if position.realised_pnl_eur:
        lines.append(
            f"- Already realised on earlier sells: {_eur(position.realised_pnl_eur)}."
        )
    lines.append(
        f"- Portfolio context: {_eur(total)} total including {_eur(cash)} cash."
    )
    relevant = [gap for gap in gaps if gap.startswith(security.ticker)]
    if relevant:
        lines.append(f"- Pricing problems: {'; '.join(relevant)}.")
    return "\n".join(lines)


def _concentration_report(db_path: Path, arguments: dict) -> ToolResult:
    """Render grouped weights with the configured limits alongside."""
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
    unpriced = [row.position.security.ticker for row in rows if row.value_eur is None]

    table = _table(
        ["Group", "Value", "Weight", "Over limit"],
        [
            [
                group.label,
                _eur(group.value_eur),
                _pct(group.weight_pct),
                "yes" if group.over_limit else "no",
            ]
            for group in groups
        ],
    )
    lines = [
        f"Weights by {group_by}, against a {_eur(total)} total that includes "
        f"{_eur(cash)} cash.",
        "",
        table,
        "",
        f"Limits: one position may not exceed "
        f"{_pct(MAX_POSITION_WEIGHT_PCT)} of the portfolio, and a group above "
        f"{_pct(CONCENTRATION_ALERT_PCT)} raises a concentration alert.",
    ]
    if group_by == "theme":
        lines.append(
            "Theme weights may sum past 100%: a security carries several "
            "themes, and splitting its value between them would understate "
            "every one of them."
        )
    if unpriced:
        lines.append(
            f"Excluded from every weight because nothing can price them: "
            f"{', '.join(unpriced)}."
        )
    return ToolResult(
        text="\n".join(lines),
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
