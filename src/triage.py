"""Ranking every holding by what deserves a deep look this week.

Triage is one model call for the whole portfolio, because the judgement is
comparative: what matters this week only means something against the rest. Its
inputs are also what the decision stage reads about each holding, including
whether the deterministic rules would allow adding to it or selling it.

Public API:
    TriageInput   -- everything triage knows about one holding
    triage_inputs -- assemble what triage knows about every holding
    run_triage    -- rank every holding by what deserves depth this week

Example:
    from triage import run_triage

    run_id, rankings, note = run_triage(conn)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from config import TRIAGE_HORIZON_DAYS, TRIAGE_LOOKBACK_DAYS, TRIAGE_MAX_TOKENS
from llm import call_llm
from research import _string_list, extract_json, load_prompt
from store_research import create_llm_trace, create_research_run

logger = logging.getLogger(__name__)


TRIAGE_PROMPT_VERSION = "triage/3"


@dataclass(frozen=True)
class TriageInput:
    """Everything triage knows about one holding before ranking it."""

    ticker: str
    name: str
    security_id: int
    weight_pct: float | None
    unrealised_return_pct: float | None
    price_move: str | None
    thesis_summary: str | None
    thesis_conviction: str
    thesis_status: str
    breaking_conditions: tuple[str, ...]
    open_questions: tuple[str, ...]
    upcoming: tuple[str, ...]
    recent: tuple[str, ...]
    estimate_change: str | None
    sell_permitted: bool | None = None
    """Whether the deterministic rules would allow selling this holding.

    Stated rather than implied. The prompt asks the model not to propose a sale
    the rules will refuse, and it proposed one anyway — reasoning from the
    owner's own remark that a thesis had lapsed, which is an opinion the owner
    holds rather than a finding research has confirmed. Worse, the refusal came
    after the fact, leaving a second recommendation referring to a sale that
    never happened. A fact in the input is harder to overlook than a rule in
    the instructions.
    """
    sell_refusal: str | None = None
    """Why selling would be refused, naming the missing sell path."""
    add_permitted: bool | None = None
    """Whether the rules would allow adding, stated for the same reason."""
    add_refusal: str | None = None
    """Why adding would be refused, naming each failing buy check."""

    def render(self) -> str:
        """Format this holding for the triage prompt."""
        lines = [f"### {self.ticker} — {self.name}"]
        facts = []
        if self.weight_pct is not None:
            facts.append(f"weight {self.weight_pct:.1f}%")
        if self.unrealised_return_pct is not None:
            facts.append(f"unrealised {self.unrealised_return_pct:+.1f}% since bought")
        if self.price_move:
            facts.append(self.price_move)
        lines.append("- " + " · ".join(facts) if facts else "- no position data")

        if self.thesis_summary:
            lines.append(
                f"- thesis ({self.thesis_conviction}, {self.thesis_status}): "
                f"{self.thesis_summary}"
            )
        else:
            lines.append("- thesis: none recorded")
        for label, items in (
            ("would break it", self.breaking_conditions),
            ("open questions", self.open_questions),
        ):
            if items:
                lines.append(f"- {label}: " + "; ".join(items))
        if self.sell_permitted is not None:
            lines.append(
                "- selling: "
                + (
                    "permitted"
                    if self.sell_permitted
                    else (
                        f"NOT permitted — "
                        f"{self.sell_refusal or f'thesis is {self.thesis_status!r}'}"
                        f" A TRIM or EXIT here will be refused."
                    )
                )
            )
        if self.add_permitted is not None:
            lines.append(
                "- adding: "
                + (
                    "permitted"
                    if self.add_permitted
                    else f"NOT permitted — {self.add_refusal} An ADD here will be refused."
                )
            )
        if self.upcoming:
            lines.append("- upcoming: " + "; ".join(self.upcoming))
        if self.recent:
            lines.append("- since last look: " + "; ".join(self.recent))
        lines.append(
            f"- consensus: {self.estimate_change or 'no comparison available'}"
        )
        return "\n".join(lines)


def _price_move(conn: sqlite3.Connection, security_id: int) -> str | None:
    """Describe the stored price change, or None when there is too little history.

    Week one has a single price per holding and no history to compare against.
    Saying so is better than computing a change from one point and implying a
    trend that was never observed.
    """
    rows = conn.execute(
        """
        SELECT price_date, close_native FROM prices
        WHERE security_id = ? ORDER BY price_date DESC LIMIT 2
        """,
        (security_id,),
    ).fetchall()
    if len(rows) < 2 or not rows[1]["close_native"]:
        return None
    newest, previous = rows[0], rows[1]
    change = (newest["close_native"] / previous["close_native"] - 1) * 100
    return f"{change:+.1f}% between {previous['price_date']} and {newest['price_date']}"


def _estimate_change(
    conn: sqlite3.Connection,
    security_id: int,
    *,
    window_days: int = TRIAGE_LOOKBACK_DAYS,
) -> str | None:
    """Describe how analyst expectations moved across the recent window.

    Comparing only the last two observations turns a feed that jumps one day
    and returns the next into a large one-day revision, and triage will then
    send research chasing the cause of a cut that never happened. The whole
    path since the window opened is described instead, with a reversal named.

    Args:
        conn: Open database connection.
        security_id: Security whose consensus to describe.
        window_days: How far before the newest observation the comparison
            starts. The last observation at or before that date is the
            baseline, so sparse history still yields a comparison.

    Returns:
        A one-line description, or None when there is nothing to compare.
    """
    rows = conn.execute(
        """
        SELECT observed_date, eps_avg FROM consensus_estimates
        WHERE security_id = ? AND eps_avg IS NOT NULL
        ORDER BY observed_date
        """,
        (security_id,),
    ).fetchall()
    if len(rows) < 2:
        return None
    start = (
        date.fromisoformat(rows[-1]["observed_date"]) - timedelta(days=window_days)
    ).isoformat()
    baseline = max(
        (i for i, row in enumerate(rows) if row["observed_date"] <= start), default=0
    )
    path: list[tuple[str, float]] = []
    for row in rows[baseline:]:
        if not path or row["eps_avg"] != path[-1][1]:
            path.append((row["observed_date"], row["eps_avg"]))
    since = path[0][0]
    if len(path) == 1:
        return f"EPS estimate unchanged since {since}"
    if any(not value for _, value in path[:-1]):
        return None

    def verb(change: float) -> str:
        return "raised" if change > 0 else "cut" if change < 0 else "unchanged"

    net = (path[-1][1] / path[0][1] - 1) * 100
    if len(path) == 2:
        return f"EPS estimate {verb(net)} {abs(net):.1f}% since {since}"
    moves = [
        (when, (value / previous - 1) * 100)
        for (_, previous), (when, value) in zip(path, path[1:])
    ]
    steps = ", ".join(
        f"{verb(change)} {abs(change):.1f}% on {when}" for when, change in moves
    )
    if all(change > 0 for _, change in moves) or all(change < 0 for _, change in moves):
        return f"EPS estimate {verb(net)} {abs(net):.1f}% since {since} ({steps})"
    return (
        f"EPS estimate net {verb(net)} {abs(net):.1f}% since {since}, but it reversed "
        f"along the way ({steps}); a move that reverses within days may be feed "
        f"noise rather than an analyst revision"
    )


def triage_inputs(
    conn: sqlite3.Connection,
    *,
    account_id: int = 1,
    horizon_days: int = TRIAGE_HORIZON_DAYS,
    lookback_days: int = TRIAGE_LOOKBACK_DAYS,
    today: date | None = None,
    include_sell_eligibility: bool = False,
) -> list[TriageInput]:
    """Assemble what triage needs to know about every holding.

    Args:
        conn: Open database connection.
        account_id: Account to triage.
        horizon_days: How far ahead an event counts as upcoming.
        lookback_days: How far back an event counts as recent.
        today: Reference date, for tests.
        include_sell_eligibility: State whether the deterministic rules would
            currently allow selling each holding. Wanted by the decision stage,
            which must not propose a sale that will be refused; pointless for
            triage, which only ranks.

    Returns:
        One entry per holding, ordered by descending weight.
    """
    from portfolio import cash_eur, holdings, total_value
    from store import load_events
    from store_research import active_theses

    now = today or date.today()
    rows = holdings(conn, account_id=account_id)
    total = total_value(rows, cash=cash_eur(conn, account_id=account_id))
    theses = active_theses(conn)

    upcoming_by_security: dict[int, list[str]] = {}
    recent_by_security: dict[int, list[str]] = {}
    for event in load_events(
        conn,
        start=(now - timedelta(days=lookback_days)).isoformat(),
        end=(now + timedelta(days=horizon_days)).isoformat(),
    ):
        if event["security_id"] is None:
            continue
        when = date.fromisoformat(event["event_date"])
        days = (when - now).days
        marker = "~" if event["confidence"] == "estimated" else ""
        label = f"{event['title']}{marker} ({'in ' if days >= 0 else ''}{abs(days)}d{' ago' if days < 0 else ''})"
        target = upcoming_by_security if days >= 0 else recent_by_security
        target.setdefault(int(event["security_id"]), []).append(label)

    sell_by_ticker: dict[str, tuple[bool, str | None]] = {}
    add_by_ticker: dict[str, tuple[bool, str | None]] = {}
    if include_sell_eligibility:
        from config import MAX_NEW_TRADE_EUR
        from guardrails import check_proposal

        from decisions import build_guardrail_context

        context = build_guardrail_context(conn, account_id=account_id, today=now)
        for row in rows:
            ticker = row.position.security.ticker
            sale = check_proposal(
                action="TRIM", ticker=ticker, amount_eur=None, context=context
            )
            sell_by_ticker[ticker] = (not sale.refused, sale.refusal)
            purchase = check_proposal(
                action="ADD",
                ticker=ticker,
                amount_eur=MAX_NEW_TRADE_EUR,
                context=context,
            )
            add_by_ticker[ticker] = (not purchase.refused, purchase.refusal)

    result: list[TriageInput] = []
    for row in rows:
        security = row.position.security
        assert security.id is not None
        thesis = theses.get(security.id)
        sale = sell_by_ticker.get(security.ticker)
        purchase = add_by_ticker.get(security.ticker)
        result.append(
            TriageInput(
                ticker=security.ticker,
                name=security.name,
                security_id=security.id,
                weight_pct=(row.value_eur / total * 100)
                if row.value_eur and total
                else None,
                unrealised_return_pct=row.unrealised_return_pct,
                price_move=_price_move(conn, security.id),
                thesis_summary=thesis.summary if thesis else None,
                thesis_conviction=thesis.conviction if thesis else "none",
                thesis_status=thesis.thesis_status if thesis else "unexamined",
                breaking_conditions=thesis.what_would_break_it if thesis else (),
                open_questions=thesis.open_questions if thesis else (),
                upcoming=tuple(upcoming_by_security.get(security.id, ())),
                recent=tuple(recent_by_security.get(security.id, ())),
                estimate_change=_estimate_change(
                    conn, security.id, window_days=lookback_days
                ),
                sell_permitted=sale[0] if sale else None,
                sell_refusal=sale[1] if sale else None,
                add_permitted=purchase[0] if purchase else None,
                add_refusal=purchase[1] if purchase else None,
            )
        )
    return result


def run_triage(
    conn: sqlite3.Connection,
    *,
    account_id: int = 1,
    today: date | None = None,
) -> tuple[int, list[dict], str]:
    """Rank every holding by what deserves a full research pass this week.

    One call covering the whole portfolio rather than one per holding: the
    judgement is comparative, so it is both cheaper and better made once with
    everything visible than fourteen times in isolation.

    Args:
        conn: Open database connection.
        account_id: Account to triage.
        today: Reference date, for tests.

    Returns:
        ``(research run id, rankings, portfolio note)``.

    Raises:
        ValueError: If there is nothing to triage or the model output is
            unusable.
        LLMError: If the call fails outright.
    """

    inputs = triage_inputs(conn, account_id=account_id, today=today)
    if not inputs:
        raise ValueError("No holdings to triage.")

    run_date = (today or date.today()).isoformat()
    trace_id = create_llm_trace(conn, operation="triage", feature="triage")
    run_id = create_research_run(
        conn, run_date=run_date, kind="triage", trace_id=trace_id
    )

    body = "\n\n".join(entry.render() for entry in inputs)
    thin = sum(1 for entry in inputs if entry.thesis_summary is None)
    caveats = []
    if all(entry.price_move is None for entry in inputs):
        caveats.append(
            "No price history is stored yet, so no holding shows a price move. "
            "Do not infer that prices were flat."
        )
    if all(entry.estimate_change is None for entry in inputs):
        caveats.append(
            "Analyst estimates have only been recorded once, so no revision can "
            "be detected yet. Do not infer that estimates were unchanged."
        )
    if thin:
        caveats.append(f"{thin} holding(s) have no recorded thesis at all.")

    message = "\n\n".join(
        [
            f"Portfolio triage for {run_date}. {len(inputs)} holdings.",
            # Stated explicitly because absent data and unchanged data look
            # identical in the rendering, and a model that cannot tell them
            # apart will report calm it never observed.
            (
                "Known gaps in what you are being given:\n"
                + "\n".join(f"- {c}" for c in caveats)
            )
            if caveats
            else "",
            body,
        ]
    ).strip()

    result = call_llm(
        conn,
        feature="triage",
        messages=[
            {"role": "system", "content": load_prompt("triage.md")},
            {"role": "user", "content": message},
        ],
        prompt_version=TRIAGE_PROMPT_VERSION,
        max_tokens=TRIAGE_MAX_TOKENS,
        trace_id=trace_id,
    )
    payload = extract_json(result.text)

    by_ticker = {entry.ticker: entry for entry in inputs}
    rankings: list[dict] = []
    seen: set[str] = set()
    for raw in payload.get("rankings", []):
        if not isinstance(raw, dict):
            continue
        ticker = str(raw.get("ticker", "")).strip().upper()
        entry = by_ticker.get(ticker)
        if entry is None or ticker in seen:
            logger.warning("Triage returned an unknown or repeated ticker: %r", ticker)
            continue
        if (
            not isinstance(raw.get("selected", False), bool)
            or type(raw.get("rank", 1)) is not int
            or raw.get("rank", 1) < 1
        ):
            raise ValueError(
                "Triage needs boolean selections and positive integer ranks. Retry main.py triage."
            )
        seen.add(ticker)
        rankings.append(
            {
                "ticker": ticker,
                "security_id": entry.security_id,
                "rank": int(raw.get("rank", len(rankings) + 1)),
                "selected": bool(raw.get("selected", False)),
                "reason": str(raw.get("reason", "")).strip() or "no reason given",
                "signals": _string_list(raw.get("signals")),
            }
        )

    if not seen:
        # Filling every holding in as "omitted" here would report a triage that
        # considered nothing as though it had run, which is worse than saying
        # it failed.
        raise ValueError(
            "Triage returned no usable rankings. Nothing was considered; the "
            "run is recorded but no holding was ranked."
        )

    missing = sorted(set(by_ticker) - seen)
    if missing:
        # Every holding must appear. A holding silently dropped from the output
        # looks identical to one that was considered and passed over.
        logger.warning("Triage omitted %s; recording them as unranked", missing)
        for ticker in missing:
            rankings.append(
                {
                    "ticker": ticker,
                    "security_id": by_ticker[ticker].security_id,
                    "rank": len(rankings) + 1,
                    "selected": False,
                    "reason": "omitted by triage; not considered",
                    "signals": ("omitted",),
                }
            )

    rankings.sort(key=lambda item: item["rank"])
    with conn:
        conn.executemany(
            """
            INSERT INTO triage_result (run_id, security_id, rank, selected, reason, signals)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    item["security_id"],
                    item["rank"],
                    int(item["selected"]),
                    item["reason"],
                    json.dumps(list(item["signals"])),
                )
                for item in rankings
            ],
        )

    return run_id, rankings, str(payload.get("portfolio_note", "")).strip()
