"""Turning portfolio data into the text the chat agent reads.

Markdown, not JSON. JSON spends tokens repeating a key on every row and leaves
the reasoning implicit: ``"value_eur": null`` beside ``"unrealised_return_pct":
null`` has to be interpreted, where "UNPRICED — excluded from the total" states
it. On a fourteen-position book the same information costs roughly a third as
much this way, and is read more reliably.

One inversion worth noting against the same approach in zdrowskit, where a
missing metric is simply left out of the rendering: here a missing price is
stated as loudly as a present one. An omitted holding would read as a portfolio
that does not contain it.

Separated from the tools so that formatting is a thing that can be read, tested
and changed on its own. The tools decide what to fetch; this decides how it
reads; nothing here touches a database.

Public API:
    UNPRICED            -- how a holding with no price is marked
    eur, pct, quantity  -- number formatting
    cell, table         -- markdown table building
    rows_table          -- arbitrary query rows
    holdings_table      -- the portfolio, one row per holding
    one_holding         -- a single holding, spelled out
    concentration_table -- grouped weights
    holding_row         -- one holding as structured data
    weight_pct          -- a holding's share of the total, or None
    unpriced_warning    -- what an unpriced holding does to a total

Example:
    from chat_render import holdings_table

    text = holdings_table(
        rows, total=1400.0, cash=4.15, gaps=[], as_of="2026-09-12"
    )
"""

from __future__ import annotations

MAX_CELL_CHARS = 300
"""Longest value rendered in one table cell before it is cut short.

The row cap bounds how many rows a query returns, not how large one is, and
``llm_call.messages_json`` holds a whole prompt in a single cell — one row of it
would crowd out the conversation it is part of. Three hundred characters is
enough to see what a value is and decide whether to ask about it specifically.
"""

UNPRICED = "UNPRICED"
"""How a holding with no price is shown.

A word rather than a blank or a dash, because it has to survive being read
quickly: the one mistake that matters here is treating it as zero.
"""

_MISSING = "—"
"""Shown where a figure cannot exist, as opposed to being unknown."""


def eur(amount: float) -> str:
    """Render a EUR amount at cent precision, thousands separated."""
    return f"€{amount:,.2f}"


def pct(value: float, *, signed: bool = False) -> str:
    """Render a percentage at one decimal, optionally with an explicit sign."""
    return f"{value:+.1f}%" if signed else f"{value:.1f}%"


def quantity(value: float) -> str:
    """Render a share count without inventing precision it does not have.

    Fractional-share brokers produce quantities like 0.0741, and 2.0 shares is
    two shares. Trailing zeros are dropped so neither reads as noise.
    """
    text = f"{value:,.4f}".rstrip("0").rstrip(".")
    return text or "0"


def cell(value: object) -> str:
    """Render one arbitrary SQL value for a markdown table.

    SQL NULL is spelled out rather than blanked: a query about what is missing
    is a normal question, and an empty cell would read as a rendering gap. A
    pipe or newline inside a value is neutralised so one trade note cannot break
    the table it sits in.
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
    if len(text) > MAX_CELL_CHARS:
        return f"{text[:MAX_CELL_CHARS]}… [{len(text)} chars, truncated]"
    return text


def table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a markdown pipe table, header once and values after."""
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def rows_table(columns: list[str], rows: list[dict], limit: int) -> str:
    """Render query rows, saying plainly when there are none or too many.

    Args:
        columns: Column names in the order the query asked for them.
        rows: The rows themselves.
        limit: The row cap that was applied, so hitting it can be declared.

    Returns:
        A header line and a table, or a sentence saying nothing matched.
    """
    if not rows:
        return "No rows. The query ran and matched nothing."

    body = table(
        columns, [[cell(row.get(column)) for column in columns] for row in rows]
    )
    header = f"{len(rows)} row{'s' if len(rows) != 1 else ''}."
    if len(rows) >= limit:
        # The model cannot tell a complete answer from a truncated one, and a
        # truncated one silently becomes "that is all of them".
        header += (
            f" This is the {limit}-row limit, so there may be more. Aggregate, "
            f"or narrow the query, before concluding anything about totals or "
            f"counts."
        )
    return f"{header}\n\n{body}"


def weight_pct(holding, total: float) -> float | None:  # type: ignore[no-untyped-def]
    """Return a holding's weight, or None when it cannot be valued.

    Given to the model rather than left to it: with only a value and a total it
    divides, and picks its own denominator when one is missing.
    """
    if holding.value_eur is None or total <= 0:
        return None
    return holding.value_eur / total * 100.0


def priced_label(holding) -> str:  # type: ignore[no-untyped-def]
    """Say when and from where a holding was priced."""
    if holding.value_eur is None:
        return "never"
    date_part = holding.price_date or "undated"
    return (
        f"{date_part} ({holding.price_source})" if holding.price_source else date_part
    )


def holding_cells(holding, total: float) -> list[str]:  # type: ignore[no-untyped-def]
    """Render one holding as a table row."""
    position = holding.position
    weight = weight_pct(holding, total)
    avg = position.avg_cost_eur
    return [
        position.security.ticker,
        quantity(position.quantity),
        eur(avg) if avg is not None else _MISSING,
        eur(position.cost_basis_eur),
        eur(holding.value_eur) if holding.value_eur is not None else UNPRICED,
        (
            pct(holding.unrealised_return_pct, signed=True)
            if holding.unrealised_return_pct is not None
            else _MISSING
        ),
        pct(weight) if weight is not None else _MISSING,
        priced_label(holding),
    ]


HOLDING_HEADERS: list[str] = [
    "Ticker",
    "Qty",
    "Avg cost",
    "Cost basis",
    "Value",
    "Return",
    "Weight",
    "Priced",
]
"""Columns of the holdings table, in reading order: what, how much, worth what."""


def holdings_table(
    rows: list,
    *,
    total: float,
    cash: float,
    pricing_notes: list[str],
    as_of: str,
) -> str:
    """Render the whole portfolio as a table with its totals spelled out.

    Args:
        rows: Valued holdings, already ordered.
        total: Portfolio value including cash.
        cash: Uninvested EUR.
        pricing_notes: Grouped valuation problems, from
            ``quality.valuation_summary``. Grouped rather than per holding
            because a sync a week behind otherwise produces two clauses for
            every line in the book, and the model reads that as noise.
        as_of: ISO date the figures are for.

    Returns:
        Markdown: one line of context, the table, then cash, total and caveats.
    """
    unpriced = [row.position.security.ticker for row in rows if row.value_eur is None]
    priced = len(rows) - len(unpriced)
    lines = [
        f"Portfolio as of {as_of} — "
        f"{len(rows)} open position{'s' if len(rows) != 1 else ''}.",
        "",
        table(HOLDING_HEADERS, [holding_cells(row, total) for row in rows]),
        "",
        f"Cash: {eur(cash)}.",
        f"Total: {eur(total)} — cash plus {priced} priced "
        f"holding{'s' if priced != 1 else ''}.",
    ]
    lines.extend(unpriced_warning(unpriced))
    if pricing_notes:
        # Stated even when everything has a value: a price five days old still
        # produces a number, and a number nobody flagged as old is the way a
        # review quietly reviews last month.
        lines.extend(["", "Valuation caveats: " + " ".join(pricing_notes)])
    return "\n".join(lines)


def unpriced_warning(unpriced: list[str]) -> list[str]:
    """State what could not be priced, and what that does to the total."""
    if not unpriced:
        return []
    return [
        "",
        f"{UNPRICED}: {', '.join(unpriced)}. Excluded from the total and from "
        f"every weight above, so the total is a floor and not what the "
        f"portfolio is worth. Never treat an unpriced holding as zero — say it "
        f"cannot be valued.",
    ]


def holding_row(holding, total: float) -> dict:  # type: ignore[no-untyped-def]
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
        "weight_pct": weight_pct(holding, total),
    }


def one_holding(  # type: ignore[no-untyped-def]
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
    weight = weight_pct(holding, total)
    avg = position.avg_cost_eur

    lines = [
        f"{security.ticker} — {security.name}",
        f"- Sector: {security.sector or 'unclassified'}; "
        f"{security.asset_class}; quoted in {security.currency}; "
        f"priced by {security.pricing_mode}.",
        f"- Holding: {quantity(position.quantity)} units at an average "
        f"{eur(avg) if avg is not None else 'unknown'} each, "
        f"{eur(position.cost_basis_eur)} invested including fees.",
    ]
    if holding.value_eur is None:
        lines.append(
            f"- Value: {UNPRICED}. Nothing can price it right now, so it is "
            f"excluded from the {eur(total)} portfolio total and has no "
            f"weight. Do not report it as zero."
        )
    else:
        lines.append(
            f"- Value: {eur(holding.value_eur)}, "
            f"{pct(holding.unrealised_return_pct or 0.0, signed=True)} "
            f"unrealised ({eur(holding.unrealised_pnl_eur or 0.0)}), "
            f"{pct(weight or 0.0)} of the portfolio."
        )
        lines.append(f"- Priced: {priced_label(holding)}.")
    if position.realised_pnl_eur:
        lines.append(
            f"- Already realised on earlier sells: {eur(position.realised_pnl_eur)}."
        )
    lines.append(f"- Portfolio context: {eur(total)} total including {eur(cash)} cash.")
    relevant = [gap for gap in gaps if gap.startswith(security.ticker)]
    if relevant:
        lines.append(f"- Pricing problems: {'; '.join(relevant)}.")
    return "\n".join(lines)


def concentration_table(
    groups: list,
    *,
    group_by: str,
    total: float,
    cash: float,
    unpriced: list[str],
    max_weight_pct: float,
    alert_pct: float,
) -> str:
    """Render grouped weights with the limits they are judged against.

    The limits travel with the numbers so the model does not invent a threshold
    of its own, and the theme caveat is stated rather than left to be noticed.

    Args:
        groups: ``ConcentrationRow`` values, already ordered.
        group_by: ``security``, ``sector`` or ``theme``.
        total: Portfolio value the weights are against.
        cash: Uninvested EUR inside that total.
        unpriced: Tickers excluded because nothing can price them.
        max_weight_pct: Single-position limit.
        alert_pct: Group weight that raises a concentration alert.

    Returns:
        Markdown: context, the table, then the limits.
    """
    lines = [
        f"Weights by {group_by}, against a {eur(total)} total that includes "
        f"{eur(cash)} cash.",
        "",
        table(
            ["Group", "Value", "Weight", "Over limit"],
            [
                [
                    group.label,
                    eur(group.value_eur),
                    pct(group.weight_pct),
                    "yes" if group.over_limit else "no",
                ]
                for group in groups
            ],
        ),
        "",
        f"Limits: one position may not exceed {pct(max_weight_pct)} of the "
        f"portfolio, and a group above {pct(alert_pct)} raises a concentration "
        f"alert.",
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
    return "\n".join(lines)
