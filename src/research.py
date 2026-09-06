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
from datetime import date

from config import PROMPTS_DIR
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
                max_tokens=3000,
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
