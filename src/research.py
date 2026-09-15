"""Turning what the owner believes into structured, trackable theses.

Bootstrapping is deliberately a restatement rather than research. The owner's
own reasons — including the weak ones — are the baseline every later comparison
is made against, so a model that improves on them destroys the thing being
measured.

Public API:
    bootstrap_theses  -- create an initial thesis per holding from context files
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
from datetime import date

from config import (
    ANALYST_MAX_TOKENS,
    PROMPTS_DIR,
    RESEARCH_ASSET_CLASSES,
    THESIS_MAX_TOKENS,
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
    model: str | None = None,
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
        model=model,
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
    model_overrides: dict[str, str] | None = None,
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
        evidence_file: Local dated excerpts matched to the planned questions.
        model_overrides: Model per feature for this run only, as returned by
            ``model_prefs.parse_overrides``. Nothing is persisted.

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

    routing = model_overrides or {}
    questions, not_this_week = _plan_research(
        conn,
        security=security,
        thesis=thesis,
        trigger=trigger,
        trace_id=trace_id,
        run_date=run_date,
        model=routing.get("plan"),
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
        model=routing.get("analyst"),
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
    revised_summary = (
        proposed_summary.strip()
        if isinstance(proposed_summary, str)
        and proposed_summary.strip()
        and proposed_summary.strip() != thesis.summary.strip()
        else None
    )
    proposed_version = None

    # New open questions alone are not a revision. They stay on the
    # assessment, which the decision stage reads, rather than asking the owner
    # to approve a thesis whose reason did not change.
    if status != "unchanged" or triggered or revised_summary:
        proposed = _propose_thesis(
            conn,
            thesis=thesis,
            status=status,
            summary=revised_summary or thesis.summary,
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
