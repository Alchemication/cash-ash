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
from dataclasses import dataclass, replace
from datetime import date, timedelta

from config import PROMPTS_DIR, THESIS_MAX_TOKENS
from llm import call_llm
from models import Thesis
from portfolio import positions
from store_research import active_thesis, create_research_run, save_thesis

logger = logging.getLogger(__name__)

PROMPT_VERSION = "thesis_bootstrap/1"

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

    depth = 0
    for index in range(start, len(stripped)):
        if stripped[index] == "{":
            depth += 1
        elif stripped[index] == "}":
            depth -= 1
            if depth == 0:
                candidate = stripped[start : index + 1]
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Malformed JSON in model output: {exc}") from exc
                if not isinstance(parsed, dict):
                    raise ValueError("Model returned JSON that is not an object.")
                return parsed
    raise ValueError("Unterminated JSON object in model output.")


def _string_list(value: object) -> tuple[str, ...]:
    """Coerce a model-supplied list into clean strings."""
    if not isinstance(value, list):
        return ()
    cleaned = [str(item).strip() for item in value if str(item).strip()]
    return tuple(cleaned[:_MAX_LIST_ENTRIES])


def _read_context(profile) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """Read the owner's context files, skipping untouched templates."""
    from profiles import is_stub

    context: dict[str, str] = {}
    for name in ("log.md", "strategy.md", "investor.md"):
        path = profile.context_path(name)
        if path.exists() and not is_stub(path):
            context[name] = path.read_text(encoding="utf-8")
    return context


def bootstrap_theses(
    conn,  # type: ignore[no-untyped-def]
    *,
    profile,  # type: ignore[no-untyped-def]
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
    context = _read_context(profile)
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


def _bootstrap_message(security, context: dict[str, str]) -> str:  # type: ignore[no-untyped-def]
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

TRIAGE_PROMPT_VERSION = "triage/2"


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
        if self.upcoming:
            lines.append("- upcoming: " + "; ".join(self.upcoming))
        if self.recent:
            lines.append("- since last look: " + "; ".join(self.recent))
        lines.append(
            f"- consensus: {self.estimate_change or 'no comparison available'}"
        )
        return "\n".join(lines)


def _price_move(conn, security_id: int) -> str | None:  # type: ignore[no-untyped-def]
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


def _estimate_change(conn, security_id: int) -> str | None:  # type: ignore[no-untyped-def]
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
    conn,  # type: ignore[no-untyped-def]
    *,
    account_id: int = 1,
    horizon_days: int = 21,
    lookback_days: int = 14,
    today: date | None = None,
) -> list[TriageInput]:
    """Assemble what triage needs to know about every holding.

    Args:
        conn: Open database connection.
        account_id: Account to triage.
        horizon_days: How far ahead an event counts as upcoming.
        lookback_days: How far back an event counts as recent.
        today: Reference date, for tests.

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
            )
        )
    return result


def run_triage(
    conn,  # type: ignore[no-untyped-def]
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
        max_tokens=THESIS_MAX_TOKENS,
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

PLAN_PROMPT_VERSION = "research_plan/1"
ANALYST_PROMPT_VERSION = "research_analyst/1"

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


def _plan_research(conn, *, security, thesis, trigger, trace_id):  # type: ignore[no-untyped-def]
    """Choose this week's questions for one holding."""
    lines = [
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
    conn,  # type: ignore[no-untyped-def]
    *,
    ticker: str,
    trigger: str = "requested directly",
    account_id: int = 1,
    today: date | None = None,
    source=None,  # type: ignore[no-untyped-def]
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
        conn, security=security, thesis=thesis, trigger=trigger, trace_id=trace_id
    )
    if not questions:
        raise ValueError(f"The planner produced no questions for {security.ticker}.")

    items = (source or get_evidence_source()).fetch(security.price_symbol, limit=8)

    body = [
        f"Holding: {security.ticker} ({security.name}).",
        f"Their thesis: {thesis.summary}",
    ]
    if thesis.what_would_break_it:
        body.append(
            "They said it would break if: " + "; ".join(thesis.what_would_break_it)
        )
    body += ["", "Questions to answer:"]
    body += [f"{index}. {question}" for index, question in enumerate(questions, 1)]
    body += ["", f"Evidence ({len(items)} items):"]
    body += (
        [item.render() for item in items]
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
        max_tokens=THESIS_MAX_TOKENS,
        trace_id=trace_id,
    )
    payload = extract_json(result.text)

    answers = [a for a in payload.get("answers", []) if isinstance(a, dict)]
    stored, sourced = _store_evidence(
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
    new_questions = _string_list(payload.get("new_open_questions"))
    proposed_summary = payload.get("proposed_summary")
    proposed_version = None

    if (
        status != "unchanged"
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


def _store_evidence(
    conn, *, run_id: int, security_id: int, answers: list[dict]
) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    """Persist an analyst's claims, keeping provenance honest.

    A claim marked sourced without a URL and a date is demoted to background
    rather than rejected. The claim may still be true and worth keeping; what
    it may not do is carry a provenance it does not have. The database would
    refuse it either way, so demoting here makes the reason legible instead of
    surfacing as a constraint error.

    Args:
        conn: Open database connection.
        run_id: Research run the claims belong to.
        security_id: Holding they concern.
        answers: Raw answer objects from the model.

    Returns:
        ``(claims stored, of which sourced)``.
    """
    from datetime import datetime, UTC

    rows: list[tuple] = []
    sourced = 0
    now = datetime.now(UTC).isoformat()

    for answer in answers:
        claim = str(answer.get("answer", "")).strip()
        if not claim:
            continue
        kind = str(answer.get("kind", "background")).strip().lower()
        url = answer.get("source_url")
        published = answer.get("published_date")
        url = url.strip() if isinstance(url, str) and url.strip() else None
        published = (
            published.strip()[:10]
            if isinstance(published, str) and published.strip()
            else None
        )

        if kind == "sourced" and not (url and published):
            logger.warning(
                "Claim marked sourced without a URL and date; storing as "
                "background: %s",
                claim[:80],
            )
            kind = "background"
        if kind not in {"sourced", "background"}:
            kind = "background"
        if kind == "background":
            url = published = None
        else:
            sourced += 1

        rows.append(
            (
                run_id,
                security_id,
                claim,
                url,
                str(answer.get("question", "")).strip() or None,
                published,
                kind,
                now,
            )
        )

    if rows:
        with conn:
            conn.executemany(
                """
                INSERT INTO evidence (
                    research_run_id, security_id, claim, source_url,
                    source_title, published_date, kind, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
    return len(rows), sourced


def _propose_thesis(  # type: ignore[no-untyped-def]
    conn,
    *,
    thesis,
    status: str,
    summary: str,
    reason: str,
    new_questions: tuple[str, ...],
    run_id: int,
    llm_call_id: int | None,
):
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
                json.dumps(list(new_questions) or list(thesis.open_questions)),
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


# ---------------------------------------------------------------------------
# Portfolio decision
# ---------------------------------------------------------------------------

DECIDE_PROMPT_VERSION = "decide/1"

_VALID_ACTIONS = {"BUY", "ADD", "HOLD", "TRIM", "EXIT", "REVIEW", "KEEP_CASH"}
_VALID_URGENCY = {"low", "medium", "high"}


def build_guardrail_context(conn, *, profile=None, account_id: int = 1):  # type: ignore[no-untyped-def]
    """Assemble the portfolio state the deterministic checks run against."""
    from config import DEFAULT_MONTHLY_CONTRIBUTION_EUR
    from guardrails import GuardrailContext
    from portfolio import cash_eur, holdings, total_value
    from store_research import active_theses

    rows = holdings(conn, account_id=account_id)
    cash = cash_eur(conn, account_id=account_id)
    total = total_value(rows, cash=cash)
    theses = active_theses(conn)

    weights: dict[str, float] = {}
    values: dict[str, float] = {}
    statuses: dict[str, str] = {}
    for row in rows:
        security = row.position.security
        assert security.id is not None
        if row.value_eur is not None and total:
            weights[security.ticker] = row.value_eur / total * 100
            values[security.ticker] = row.value_eur
        thesis = theses.get(security.id)
        statuses[security.ticker] = thesis.thesis_status if thesis else "unexamined"

    return GuardrailContext(
        total_value_eur=total,
        cash_eur=cash,
        monthly_contribution_eur=(
            profile.monthly_contribution_eur
            if profile is not None
            else DEFAULT_MONTHLY_CONTRIBUTION_EUR
        ),
        weights_by_ticker=weights,
        values_by_ticker=values,
        thesis_status_by_ticker=statuses,
    )


def run_decision(
    conn,  # type: ignore[no-untyped-def]
    *,
    profile=None,  # type: ignore[no-untyped-def]
    account_id: int = 1,
    today: date | None = None,
) -> tuple[int, list[dict], str]:
    """Propose recommendations, then enforce the deterministic rules on them.

    The model proposes; :mod:`guardrails` decides. A proposal that breaches a
    position cap, a trade limit or the owner's own sell discipline is refused
    or reduced here rather than argued with in a prompt, which is the reason a
    model is allowed near this decision at all.

    Every surviving recommendation records the price and FX rate at the time.
    That is the forward-tracking the evaluation rests on and it cannot be
    reconstructed later.

    Args:
        conn: Open database connection.
        profile: Profile supplying the contribution figure.
        account_id: Account to decide for.
        today: Reference date, for tests.

    Returns:
        ``(research run id, stored recommendations, summary)``.

    Raises:
        ValueError: If there is nothing to decide on, or output is unusable.
    """
    from config import RECOMMENDATION_EXPIRY_DAYS
    from guardrails import check_proposal
    from store import latest_prices, load_securities
    from store_research import create_research_run

    inputs = triage_inputs(conn, account_id=account_id, today=today)
    if not inputs:
        raise ValueError("No holdings to decide on.")

    context = build_guardrail_context(conn, profile=profile, account_id=account_id)
    run_date = (today or date.today()).isoformat()
    run_id = create_research_run(
        conn, run_date=run_date, kind="deep", note="Portfolio decision"
    )

    message = "\n\n".join(
        [
            f"Portfolio decision for {run_date}.",
            f"Total EUR {context.total_value_eur:,.2f} · cash EUR "
            f"{context.cash_eur:,.2f} · new money this month EUR "
            f"{context.monthly_contribution_eur:,.2f}.",
            "\n\n".join(entry.render() for entry in inputs),
        ]
    )

    result = call_llm(
        conn,
        feature="decision",
        messages=[
            {"role": "system", "content": load_prompt("decide.md")},
            {"role": "user", "content": message},
        ],
        prompt_version=DECIDE_PROMPT_VERSION,
        max_tokens=THESIS_MAX_TOKENS,
    )
    payload = extract_json(result.text)

    securities = load_securities(conn)
    prices = latest_prices(conn)
    expires = (
        (today or date.today()) + timedelta(days=RECOMMENDATION_EXPIRY_DAYS)
    ).isoformat()

    stored: list[dict] = []
    allocated = 0.0

    for raw in payload.get("recommendations", []):
        if not isinstance(raw, dict):
            continue
        action = str(raw.get("action", "")).strip().upper()
        if action not in _VALID_ACTIONS:
            logger.warning("Discarding proposal with unknown action %r", action)
            continue
        # HOLD carries no instruction and no consequence; storing one per
        # untouched holding would bury the few rows that mean something.
        if action == "HOLD":
            continue

        ticker = raw.get("ticker")
        ticker = str(ticker).strip().upper() if ticker else None
        if ticker and ticker not in securities:
            logger.warning("Discarding proposal for unknown ticker %r", ticker)
            continue

        amount = raw.get("amount_eur")
        try:
            amount = float(amount) if amount is not None else None
        except (TypeError, ValueError):
            amount = None

        verdict = check_proposal(
            action=action,
            ticker=ticker,
            amount_eur=amount,
            context=replace(context, allocated_this_run_eur=allocated),
        )
        if verdict.refused:
            logger.info("Guardrail refused %s %s: %s", action, ticker, verdict.refusal)
            stored.append(
                {
                    "id": None,
                    "ticker": ticker,
                    "action": action,
                    "amount_eur": None,
                    "refused": True,
                    "refusal": verdict.refusal,
                    "rationale": str(raw.get("rationale", "")).strip(),
                    "urgency": "low",
                    "adjustments": (),
                }
            )
            continue

        if verdict.amount_eur and verdict.action in {"BUY", "ADD"}:
            allocated += verdict.amount_eur

        urgency = str(raw.get("urgency", "low")).strip().lower()
        security = securities.get(ticker) if ticker else None
        price_row = prices.get(security.id) if security and security.id else None

        recommendation_id = _store_recommendation(
            conn,
            run_date=run_date,
            run_id=run_id,
            security=security,
            action=verdict.action,
            amount_eur=verdict.amount_eur,
            rationale=str(raw.get("rationale", "")).strip() or "no rationale given",
            urgency=urgency if urgency in _VALID_URGENCY else "low",
            price_row=price_row,
            weight_pct=context.weights_by_ticker.get(ticker) if ticker else None,
            expires_on=expires,
            verdict=verdict,
            llm_call_id=result.llm_call_id,
        )
        stored.append(
            {
                "id": recommendation_id,
                "ticker": ticker,
                "action": verdict.action,
                "amount_eur": verdict.amount_eur,
                "refused": False,
                "refusal": None,
                "rationale": str(raw.get("rationale", "")).strip(),
                "urgency": urgency if urgency in _VALID_URGENCY else "low",
                "adjustments": verdict.adjustments,
            }
        )

    return run_id, stored, str(payload.get("summary", "")).strip()


def _store_recommendation(  # type: ignore[no-untyped-def]
    conn,
    *,
    run_date: str,
    run_id: int,
    security,
    action: str,
    amount_eur: float | None,
    rationale: str,
    urgency: str,
    price_row,
    weight_pct: float | None,
    expires_on: str,
    verdict,
    llm_call_id: int | None,
) -> int:
    """Persist one recommendation with the price that stood behind it."""
    from datetime import UTC, datetime

    from store_research import active_thesis

    fx = None
    value_eur = None
    price_native = price_row["close_native"] if price_row is not None else None
    if price_row is not None:
        row = conn.execute(
            """
            SELECT rate FROM fx_rates WHERE base = ? AND quote = 'EUR'
            ORDER BY rate_date DESC LIMIT 1
            """,
            (price_row["currency"],),
        ).fetchone()
        fx = (
            float(row["rate"])
            if row
            else (1.0 if price_row["currency"] == "EUR" else None)
        )
        if fx is not None and price_native is not None:
            value_eur = price_native * fx

    thesis = (
        active_thesis(conn, security_id=security.id)
        if security is not None and security.id is not None
        else None
    )

    with conn:
        cursor = conn.execute(
            """
            INSERT INTO recommendation (
                run_date, research_run_id, security_id, action, amount_eur,
                rationale, urgency, thesis_id, price_native, fx_rate, value_eur,
                weight_pct, expires_on, guardrails, adjusted, llm_call_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_date,
                run_id,
                security.id if security is not None else None,
                action,
                amount_eur,
                rationale,
                urgency,
                thesis.id if thesis else None,
                price_native,
                fx,
                value_eur,
                weight_pct,
                expires_on,
                json.dumps(
                    {
                        "checks": list(verdict.checks),
                        "adjustments": list(verdict.adjustments),
                    }
                ),
                int(verdict.adjusted),
                llm_call_id,
                datetime.now(UTC).isoformat(),
            ),
        )
    return int(cursor.lastrowid)
