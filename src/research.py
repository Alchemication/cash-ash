"""Turning what the owner believes into structured, trackable theses.

Bootstrapping is deliberately a restatement rather than research. The owner's
own reasons — including the weak ones — are the baseline every later comparison
is made against, so a model that improves on them destroys the thing being
measured.

Public API:
    bootstrap_theses  -- create an initial thesis per holding from context files
    triage_inputs     -- assemble what triage knows about every holding
    run_triage        -- rank every holding by what deserves depth this week
    load_prompt       -- read a prompt file

Example:
    from research import bootstrap_theses

    created = bootstrap_theses(conn, profile=profile)
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from config import (
    ANALYST_MAX_TOKENS,
    PROMPTS_DIR,
    RESEARCH_ASSET_CLASSES,
    THESIS_MAX_TOKENS,
    TRIAGE_HORIZON_DAYS,
    TRIAGE_LOOKBACK_DAYS,
    TRIAGE_MAX_TOKENS,
)
from pathlib import Path
from llm import call_llm
from models import Security, Thesis
from profiles import Profile, read_context
from evidence import EvidenceSource
from portfolio import positions
from store_research import active_thesis, create_research_run, save_thesis

logger = logging.getLogger(__name__)

PROMPT_VERSION = "thesis_bootstrap/2"

_VALID_CONVICTION = {"none", "weak", "moderate", "strong"}
_MAX_LIST_ENTRIES = 6


def load_prompt(name: str) -> str:
    """Read a prompt file from ``src/prompts``.

    Args:
        name: Filename, e.g. ``thesis_bootstrap.md``.

    Returns:
        The prompt text.

    Raises:
        FileNotFoundError: If the prompt does not exist.
    """
    path = PROMPTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"No prompt at {path}.")
    return path.read_text(encoding="utf-8")


def extract_json(text: str) -> dict:
    """Return the first JSON object in *text*.

    Reasoning models routinely wrap their answer in prose or a fenced block
    despite being asked not to, and failing the whole run over a stray sentence
    would be a poor trade for something this easy to recover from.

    Args:
        text: Model output.

    Returns:
        The parsed object.

    Raises:
        ValueError: If no JSON object can be found or parsed.
    """
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if fenced:
        stripped = fenced.group(1)

    start = stripped.find("{")
    if start == -1:
        raise ValueError(f"No JSON object in model output: {text[:200]!r}")

    try:
        parsed, _ = json.JSONDecoder().raw_decode(stripped[start:])
    except json.JSONDecodeError as exc:
        label = (
            "Unterminated JSON object"
            if not stripped.endswith("}")
            else "Malformed JSON"
        )
        raise ValueError(f"{label} in model output: {exc}") from exc
    return parsed


def _string_list(value: object) -> tuple[str, ...]:
    """Coerce a model-supplied list into clean strings."""
    if not isinstance(value, list):
        return ()
    cleaned = [str(item).strip() for item in value if str(item).strip()]
    return tuple(cleaned[:_MAX_LIST_ENTRIES])


def bootstrap_theses(
    conn: sqlite3.Connection,
    *,
    profile: Profile,
    account_id: int = 1,
    only: str | None = None,
    overwrite: bool = False,
) -> list[tuple[str, Thesis | None, str | None]]:
    """Create an initial thesis for each holding from the owner's own notes.

    Args:
        conn: Open database connection.
        profile: Profile whose context files supply the reasons.
        account_id: Account whose holdings to bootstrap.
        only: Restrict to one ticker.
        overwrite: Replace an existing thesis with a new version.

    Returns:
        ``(ticker, thesis or None, skip reason or None)`` per holding.

    Raises:
        ValueError: If no usable context file exists, or *only* is unknown.
    """
    context = read_context(profile, names=("log.md", "strategy.md", "investor.md"))
    if "log.md" not in context:
        raise ValueError(
            f"No written log at {profile.context_path('log.md')}. A thesis is "
            f"bootstrapped from your own reasons for owning each position, so "
            f"there is nothing to build from until that file says why."
        )

    held = [position.security for position in positions(conn, account_id=account_id)]
    if only:
        wanted = only.upper()
        held = [security for security in held if security.ticker == wanted]
        if not held:
            raise ValueError(f"No holding with ticker {only!r}.")

    run_id = create_research_run(
        conn,
        run_date=date.today().isoformat(),
        kind="bootstrap",
        note="Initial theses restated from the owner's own notes.",
    )
    system = load_prompt("thesis_bootstrap.md")
    results: list[tuple[str, Thesis | None, str | None]] = []

    for security in held:
        assert security.id is not None
        existing = active_thesis(conn, security_id=security.id)
        if existing and not overwrite:
            results.append((security.ticker, existing, "already has a thesis"))
            continue

        user = _bootstrap_message(security, context)
        try:
            result = call_llm(
                conn,
                feature="plan",
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                prompt_version=PROMPT_VERSION,
                max_tokens=THESIS_MAX_TOKENS,
            )
            payload = extract_json(result.text)
        except Exception as exc:  # noqa: BLE001 - one failure must not stop the rest
            logger.warning("Bootstrap failed for %s: %s", security.ticker, exc)
            results.append((security.ticker, None, str(exc)[:120]))
            continue

        summary = str(payload.get("summary", "")).strip()
        if not summary:
            results.append((security.ticker, None, "model returned no summary"))
            continue

        conviction = str(payload.get("conviction", "unstated")).strip().lower()
        thesis = save_thesis(
            conn,
            Thesis(
                security_id=security.id,
                summary=summary,
                rationale=str(payload.get("rationale", "")).strip() or None,
                conviction=conviction
                if conviction in _VALID_CONVICTION
                else "unstated",
                # Restating a reason examines nothing. Marking these anything
                # other than unexamined would claim the position has been
                # looked at when only the sentence has.
                thesis_status="unexamined",
                key_assumptions=_string_list(payload.get("key_assumptions")),
                open_questions=_string_list(payload.get("open_questions")),
                what_would_break_it=_string_list(payload.get("what_would_break_it")),
                source="user",
                llm_call_id=result.llm_call_id,
                note=f"Bootstrapped from context files in research run {run_id}.",
            ),
        )
        results.append((security.ticker, thesis, None))

    return results


def _bootstrap_message(security: Security, context: dict[str, str]) -> str:
    """Build the user message for one security.

    The whole log is included rather than just this holding's line, so the
    model can see that two reasons are identical or that one contradicts
    another — which is exactly the kind of thing worth surfacing.
    """
    parts = [
        f"Restate the owner's reason for holding {security.ticker} "
        f"({security.name}) as a structured thesis.",
        "",
        "Their full log of reasons for every holding follows. Use only the "
        f"entry for {security.ticker}; the rest is context so you can see how "
        "their reasoning varies across positions.",
        "",
        "--- log.md ---",
        context["log.md"],
    ]
    if "strategy.md" in context:
        parts += [
            "",
            "--- strategy.md (how they say they invest) ---",
            context["strategy.md"],
        ]
    if "investor.md" in context:
        parts += ["", "--- investor.md (who they are) ---", context["investor.md"]]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------

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
                        f"NOT permitted — thesis is '{self.thesis_status}' and the "
                        f"position is within its weight cap. A TRIM or EXIT here "
                        f"will be refused."
                    )
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


def _estimate_change(conn: sqlite3.Connection, security_id: int) -> str | None:
    """Describe how analyst expectations moved, if there are two observations."""
    rows = conn.execute(
        """
        SELECT observed_date, eps_avg, revenue_avg FROM consensus_estimates
        WHERE security_id = ? AND eps_avg IS NOT NULL
        ORDER BY observed_date DESC LIMIT 2
        """,
        (security_id,),
    ).fetchall()
    if len(rows) < 2:
        return None
    newest, previous = rows[0], rows[1]
    if not previous["eps_avg"]:
        return None
    change = (newest["eps_avg"] / previous["eps_avg"] - 1) * 100
    direction = "raised" if change > 0 else "cut" if change < 0 else "unchanged"
    return (
        f"EPS estimate {direction} {abs(change):.1f}% since {previous['observed_date']}"
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

    sell_permitted_by_ticker: dict[str, bool] = {}
    if include_sell_eligibility:
        from guardrails import check_proposal

        from decisions import build_guardrail_context

        context = build_guardrail_context(conn, account_id=account_id, today=now)
        for row in rows:
            ticker = row.position.security.ticker
            sell_permitted_by_ticker[ticker] = not check_proposal(
                action="TRIM", ticker=ticker, amount_eur=None, context=context
            ).refused

    result: list[TriageInput] = []
    for row in rows:
        security = row.position.security
        assert security.id is not None
        thesis = theses.get(security.id)
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
                estimate_change=_estimate_change(conn, security.id),
                sell_permitted=sell_permitted_by_ticker.get(security.ticker),
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
    from store_research import create_research_run

    inputs = triage_inputs(conn, account_id=account_id, today=today)
    if not inputs:
        raise ValueError("No holdings to triage.")

    run_date = (today or date.today()).isoformat()
    run_id = create_research_run(conn, run_date=run_date, kind="triage")

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


# ---------------------------------------------------------------------------
# Deep research
# ---------------------------------------------------------------------------

PLAN_PROMPT_VERSION = "research_plan/2"
ANALYST_PROMPT_VERSION = "research_analyst/4"

_VALID_THESIS_STATUS = {"improving", "unchanged", "deteriorating", "broken"}


@dataclass(frozen=True)
class ResearchResult:
    """What one deep pass concluded about one holding."""

    ticker: str
    run_id: int
    questions: tuple[str, ...]
    not_this_week: tuple[str, ...]
    answers: tuple[dict, ...]
    thesis_status: str
    status_reason: str
    triggered: tuple[str, ...]
    new_open_questions: tuple[str, ...]
    proposed_version: int | None
    evidence_count: int
    sourced_count: int


def _plan_research(
    conn: sqlite3.Connection,
    *,
    security: Security,
    thesis: Thesis,
    trigger: str,
    trace_id: int,
    run_date: str,
) -> tuple[list[str], tuple[str, ...]]:
    """Choose this week's questions for one holding."""
    lines = [
        f"Research date: {run_date}.",
        f"Holding: {security.ticker} ({security.name}).",
        "",
        f"Triage selected it because: {trigger}",
        "",
        f"Their thesis: {thesis.summary}",
    ]
    if thesis.rationale:
        lines.append(f"In full: {thesis.rationale}")
    for label, items in (
        ("What they said would break it", thesis.what_would_break_it),
        ("Questions left open", thesis.open_questions),
        ("What it assumes", thesis.key_assumptions),
    ):
        if items:
            lines.append(f"{label}: " + "; ".join(items))

    result = call_llm(
        conn,
        feature="plan",
        messages=[
            {"role": "system", "content": load_prompt("research_plan.md")},
            {"role": "user", "content": "\n".join(lines)},
        ],
        prompt_version=PLAN_PROMPT_VERSION,
        max_tokens=THESIS_MAX_TOKENS,
        trace_id=trace_id,
    )
    payload = extract_json(result.text)
    questions = [
        str(item.get("question", "")).strip()
        for item in payload.get("questions", [])
        if isinstance(item, dict) and str(item.get("question", "")).strip()
    ]
    return questions[:_MAX_LIST_ENTRIES], _string_list(payload.get("not_this_week"))


def research_security(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    trigger: str = "requested directly",
    account_id: int = 1,
    today: date | None = None,
    source: EvidenceSource | None = None,
    evidence_file: Path | None = None,
) -> ResearchResult:
    """Run one deep research pass and propose — never apply — a thesis update.

    A proposal sits beside the active thesis until the owner accepts it. The
    thesis records what *they* believe, so a pipeline able to rewrite it would
    be editing the baseline it is measured against.

    Args:
        conn: Open database connection.
        ticker: Holding to research.
        trigger: Why it is being researched, passed to the planner.
        account_id: Account the holding belongs to.
        today: Reference date, for tests.
        source: Evidence source; defaults to the configured one.

    Returns:
        What the pass concluded.

    Raises:
        ValueError: If the holding or its thesis is missing, or output is
            unusable.
    """
    from evidence import get_evidence_source
    from store import load_securities
    from store_research import active_thesis, create_research_run, create_llm_trace

    securities = load_securities(conn)
    security = securities.get(ticker.upper())
    if security is None:
        known = ", ".join(sorted(securities))
        raise ValueError(f"Unknown ticker {ticker!r}. Known: {known}.")
    assert security.id is not None

    if security.asset_class not in RESEARCH_ASSET_CLASSES:
        raise ValueError(
            f"{security.ticker}: company research does not support {security.asset_class}; review manually."
        )
    thesis = active_thesis(conn, security_id=security.id)
    if thesis is None:
        raise ValueError(
            f"No thesis for {security.ticker}. Research compares evidence "
            f"against a stated reason, so there is nothing to compare to until "
            f"'main.py thesis bootstrap' has run."
        )

    run_date = (today or date.today()).isoformat()
    trace_id = create_llm_trace(
        conn, operation="deep_research", feature="analyst", reference=security.ticker
    )
    run_id = create_research_run(
        conn,
        run_date=run_date,
        kind="deep",
        trace_id=trace_id,
        note=f"Deep pass on {security.ticker}: {trigger}",
    )

    questions, not_this_week = _plan_research(
        conn,
        security=security,
        thesis=thesis,
        trigger=trigger,
        trace_id=trace_id,
        run_date=run_date,
    )
    if not questions:
        raise ValueError(f"The planner produced no questions for {security.ticker}.")

    from research_evidence import (
        gather_evidence,
        validate_answers,
        store_evidence,
        save_assessment,
    )

    items = gather_evidence(
        source or get_evidence_source(),
        security.price_symbol,
        questions,
        today=today or date.today(),
        evidence_file=evidence_file,
    )

    body = [
        f"Research date: {run_date}.",
        f"Holding: {security.ticker} ({security.name}).",
        f"Their thesis: {thesis.summary}",
    ]
    if thesis.rationale:
        body.append(f"Their rationale: {thesis.rationale}")
    if thesis.key_assumptions:
        body.append("Their assumptions: " + "; ".join(thesis.key_assumptions))
    if thesis.what_would_break_it:
        body.append(
            "They said it would break if: " + "; ".join(thesis.what_would_break_it)
        )
    body += ["", "Questions to answer:"]
    body += [f"{index}. {question}" for index, question in enumerate(questions, 1)]
    body += ["", f"Evidence ({len(items)} items):"]
    body += (
        [f"E{i}: {item.render()}" for i, item in enumerate(items, 1)]
        if items
        else [
            "None retrieved. That is an absence of evidence, not evidence that "
            "nothing happened — say so rather than concluding calm."
        ]
    )

    result = call_llm(
        conn,
        feature="analyst",
        messages=[
            {"role": "system", "content": load_prompt("research_analyst.md")},
            {"role": "user", "content": "\n".join(body)},
        ],
        prompt_version=ANALYST_PROMPT_VERSION,
        max_tokens=ANALYST_MAX_TOKENS,
        trace_id=trace_id,
    )
    payload = extract_json(result.text)

    answers = validate_answers(payload.get("answers"), items, questions)
    stored, sourced = store_evidence(
        conn, run_id=run_id, security_id=security.id, answers=answers
    )

    status = str(payload.get("thesis_status", "")).strip().lower()
    if status not in _VALID_THESIS_STATUS:
        logger.warning(
            "Analyst returned an unusable thesis status %r for %s; recording "
            "as unchanged",
            status,
            security.ticker,
        )
        status = "unchanged"

    triggered = _string_list(payload.get("breaking_conditions_triggered"))
    if any(condition not in thesis.what_would_break_it for condition in triggered):
        raise ValueError(
            "Analyst invented a breaking condition. Review the research output."
        )
    if status != "unchanged" and (
        not sourced or any(a.get("validation_error") for a in answers)
    ):
        status = "unchanged"
        triggered = ()
        payload["status_reason"] = (
            "Insufficient verified citations to assess a thesis change."
        )
    if status == "broken" and not triggered:
        status = "deteriorating"
    new_questions = _string_list(payload.get("new_open_questions"))
    proposed_summary = payload.get("proposed_summary") if sourced else None
    proposed_version = None

    if (
        status != "unchanged"
        or new_questions
        or triggered
        or (isinstance(proposed_summary, str) and proposed_summary.strip())
    ):
        proposed = _propose_thesis(
            conn,
            thesis=thesis,
            status=status,
            summary=(
                proposed_summary.strip()
                if isinstance(proposed_summary, str) and proposed_summary.strip()
                else thesis.summary
            ),
            reason=str(payload.get("status_reason", "")).strip(),
            new_questions=new_questions,
            run_id=run_id,
            llm_call_id=result.llm_call_id,
        )
        proposed_version = proposed.version

    save_assessment(
        conn,
        run_id=run_id,
        security_id=security.id,
        thesis_id=thesis.id,
        status=status,
        reason=str(payload.get("status_reason", "")),
        questions=questions,
        answers=answers,
        triggered=triggered,
        open_questions=new_questions,
        items=items,
    )
    return ResearchResult(
        ticker=security.ticker,
        run_id=run_id,
        questions=tuple(questions),
        not_this_week=not_this_week,
        answers=tuple(answers),
        thesis_status=status,
        status_reason=str(payload.get("status_reason", "")).strip(),
        triggered=triggered,
        new_open_questions=new_questions,
        proposed_version=proposed_version,
        evidence_count=stored,
        sourced_count=sourced,
    )


def _propose_thesis(
    conn: sqlite3.Connection,
    *,
    thesis: Thesis,
    status: str,
    summary: str,
    reason: str,
    new_questions: tuple[str, ...],
    run_id: int,
    llm_call_id: int | None,
) -> Thesis:
    """Record a proposed revision without adopting it.

    Deliberately does not go through ``save_thesis``: that supersedes the
    active version, which is exactly what must not happen here. The proposal
    takes the next version number and waits.
    """
    from datetime import datetime, UTC

    from store_research import _thesis_from_row

    with conn:
        row = conn.execute(
            "SELECT MAX(version) AS version FROM thesis WHERE security_id = ?",
            (thesis.security_id,),
        ).fetchone()
        version = (row["version"] or 0) + 1
        cursor = conn.execute(
            """
            INSERT INTO thesis (
                security_id, version, status, summary, rationale, conviction,
                thesis_status, key_assumptions, open_questions,
                what_would_break_it, source, supersedes_id, llm_call_id,
                research_run_id, note, created_at
            )
            VALUES (?, ?, 'proposed', ?, ?, ?, ?, ?, ?, ?, 'research', ?, ?, ?, ?, ?)
            """,
            (
                thesis.security_id,
                version,
                summary,
                reason or thesis.rationale,
                thesis.conviction,
                status,
                json.dumps(list(thesis.key_assumptions)),
                json.dumps(
                    list(dict.fromkeys((*thesis.open_questions, *new_questions)))
                ),
                json.dumps(list(thesis.what_would_break_it)),
                thesis.id,
                llm_call_id,
                run_id,
                f"Proposed by research run {run_id}. Not adopted.",
                datetime.now(UTC).isoformat(),
            ),
        )
    return _thesis_from_row(
        conn.execute(
            "SELECT * FROM thesis WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    )
