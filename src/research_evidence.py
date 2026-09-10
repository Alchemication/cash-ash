"""Question-linked evidence packages and deterministic citation checks."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from config import EVIDENCE_ITEMS_PER_SECURITY, EVIDENCE_MAX_AGE_DAYS
from evidence import EvidenceItem, EvidenceSource
from store_workflow import now_iso


def gather_evidence(
    source: EvidenceSource,
    symbol: str,
    questions: list[str],
    *,
    today: date,
    evidence_file: Path | None = None,
) -> list[EvidenceItem]:
    """Prefer owner-supplied disclosures matched to questions, then recent news.

    Curated JSON is local input, not a remote URL fetcher. Excerpts remain
    attributed material; a primary label is the owner's classification.
    """
    items: list[EvidenceItem] = []
    if evidence_file and evidence_file.exists():
        payload = json.loads(evidence_file.read_text())
        if not isinstance(payload, list):
            raise ValueError(
                "Evidence file must contain a JSON list of dated excerpts."
            )
        for raw in payload:
            if raw.get("symbol", "").upper() != symbol.upper():
                continue
            targets = raw.get("questions", [])
            if not isinstance(targets, list) or not all(
                isinstance(q, str) for q in targets
            ):
                raise ValueError("Evidence questions must be a list of question text.")
            if targets and not any(
                a.lower() in b.lower() or b.lower() in a.lower()
                for a in targets
                for b in questions
            ):
                continue
            items.append(
                EvidenceItem(
                    title=raw["title"],
                    url=raw["url"],
                    published=raw["published"],
                    publisher=raw.get("publisher"),
                    summary=raw["excerpt"],
                )
            )
    items += source.fetch(symbol, limit=EVIDENCE_ITEMS_PER_SECURITY)
    unique: dict[str, EvidenceItem] = {}
    for item in items:
        try:
            age = (today - date.fromisoformat(item.published)).days
        except ValueError:
            continue
        if not 0 <= age <= EVIDENCE_MAX_AGE_DAYS or urlparse(item.url).scheme not in {
            "http",
            "https",
        }:
            continue
        unique.setdefault(item.url, item)
    return list(unique.values())[:EVIDENCE_ITEMS_PER_SECURITY]


def validate_answers(
    answers: object, items: list[EvidenceItem], questions: list[str]
) -> list[dict]:
    """Bind sourced answers to supplied IDs and exact excerpts; preserve gaps.

    Citation membership and quote presence are checked. Semantic support is
    still a review task, never claimed as mechanically proven.
    """
    if not isinstance(answers, list) or not answers:
        raise ValueError(
            "Analyst returned no answers. Retry research with relevant evidence."
        )
    normalized: list[dict] = []
    supplied = {f"E{i}": item for i, item in enumerate(items, 1)}
    for raw in answers:
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("answer"), str)
            or not isinstance(raw.get("question"), str)
        ):
            raise ValueError("Malformed research answer. Retry research.")
        if not raw["answer"].strip():
            raise ValueError("Research answer cannot be blank.")
        answer = dict(raw)
        kind = answer.get("kind")
        if kind not in {"sourced", "background", "unanswered"}:
            raise ValueError(
                "Research answer needs sourced, background or unanswered kind."
            )
        if kind == "sourced":
            item = supplied.get(answer.get("source_id"))
            quote = answer.get("supporting_quote")
            if (
                item is None
                or not isinstance(quote, str)
                or not quote.strip()
                or quote not in f"{item.title}\n{item.summary or ''}"
            ):
                answer.update(
                    kind="unanswered",
                    answer="Citation could not be verified against the supplied excerpt.",
                    validation_error="invalid citation or excerpt",
                )
            else:
                answer.update(source_url=item.url, published_date=item.published)
        if answer["kind"] != "sourced":
            answer.update(
                source_url=None,
                published_date=None,
                source_id=None,
                supporting_quote=None,
            )
        normalized.append(answer)
    answered = {a["question"].strip().casefold() for a in normalized}
    for question in questions:
        if question.strip().casefold() not in answered:
            normalized.append(
                dict(
                    question=question,
                    answer="Question omitted by analyst.",
                    kind="unanswered",
                    source_url=None,
                    published_date=None,
                )
            )
    return normalized


def store_evidence(
    conn: sqlite3.Connection, *, run_id: int, security_id: int, answers: list[dict]
) -> tuple[int, int]:
    """Store normalized answers, including unanswered questions."""
    with conn:
        conn.executemany(
            """INSERT INTO evidence
            (research_run_id,security_id,claim,source_url,source_title,published_date,kind,created_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            [
                (
                    run_id,
                    security_id,
                    a["answer"],
                    a.get("source_url"),
                    a["question"],
                    a.get("published_date"),
                    a["kind"],
                    now_iso(),
                )
                for a in answers
            ],
        )
    return len(answers), sum(a["kind"] == "sourced" for a in answers)


def sufficient_coverage(questions: list[str], answers: list[dict]) -> bool:
    """Require sourced answers covering every planned question.

    Background remains useful explanation, but is not evidence that a current
    company question was checked. This checks coverage, not semantic support
    or whether the questions establish an attractive investment.
    """
    required = {q.strip().casefold() for q in questions if q.strip()}
    answered = {a.get("question", "").strip().casefold() for a in answers}
    return (
        bool(required)
        and required <= answered
        and bool(answers)
        and all(
            a.get("kind") == "sourced" and not a.get("validation_error")
            for a in answers
        )
    )


def save_assessment(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    security_id: int,
    thesis_id: int,
    status: str,
    reason: str,
    questions: list[str],
    answers: list[dict],
    triggered: tuple[str, ...],
    open_questions: tuple[str, ...],
    items: list[EvidenceItem],
) -> None:
    """Freeze evidence and outcomes even when the owner's thesis is unchanged."""
    package = json.dumps([asdict(item) for item in items], sort_keys=True)
    sufficient = sufficient_coverage(questions, answers)
    with conn:
        conn.execute(
            """INSERT INTO research_assessment
            (run_id,security_id,thesis_id,status,reason,questions_json,answers_json,triggered_json,
             open_questions_json,package_json,package_hash,coverage,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                security_id,
                thesis_id,
                status,
                reason,
                json.dumps(questions),
                json.dumps(answers),
                json.dumps(triggered),
                json.dumps(open_questions),
                package,
                hashlib.sha256(package.encode()).hexdigest(),
                "sufficient" if sufficient else "insufficient",
                now_iso(),
            ),
        )
