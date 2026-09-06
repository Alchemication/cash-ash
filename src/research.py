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
from dataclasses import dataclass
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
