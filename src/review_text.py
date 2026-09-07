"""Phone-sized thesis comparisons and evidence details."""

from __future__ import annotations

import json
import sqlite3

from notify import escape
from store import load_securities
from store_research import active_thesis, thesis_history
from store_workflow import latest_assessments


def thesis_text(conn: sqlite3.Connection, ticker: str) -> str:
    """Compare the active thesis with pending revisions, labelled explicitly."""
    security = load_securities(conn).get(ticker.upper())
    if security is None:
        return "Unknown ticker. Use /holdings to list your holdings."
    active = active_thesis(conn, security_id=security.id)
    lines = [
        f"<b>{escape(ticker.upper())} — owner thesis</b>",
        escape(active.summary if active else "No active thesis."),
    ]
    for proposal in thesis_history(conn, security_id=security.id):
        if proposal.status == "proposed":
            lines += [
                f"<b>Proposed v{proposal.version} — not adopted</b>",
                escape(proposal.summary),
                escape(proposal.rationale or ""),
                f"/accept {escape(ticker.upper())} {proposal.version}",
                f"/reject {escape(ticker.upper())} {proposal.version}",
            ]
    return "\n".join(lines)


def evidence_text(conn: sqlite3.Connection, ticker: str) -> str:
    """Show the latest dated findings and exact source excerpts."""
    row = next(
        (r for r in latest_assessments(conn) if r["ticker"] == ticker.upper()), None
    )
    if row is None:
        return "No completed assessment. Run main.py research TICKER first."
    lines = [
        f"<b>{escape(ticker.upper())} research — {row['run_date']}</b>",
        f"Coverage: {row['coverage']}",
        escape(row["reason"]),
    ]
    for answer in json.loads(row["answers_json"]):
        lines += [
            "",
            escape(answer["question"]),
            f"[{answer['kind']}] {escape(answer['answer'])}",
        ]
        if answer.get("source_url"):
            lines += [
                escape(answer.get("supporting_quote", "")),
                escape(answer["source_url"]),
            ]
    lines.append(
        "Citation membership and excerpt checked; judge whether they support the conclusion."
    )
    return "\n".join(lines)


def recommendation_buttons(item: dict) -> list[list[tuple[str, str]]]:
    """Use acknowledgement for review questions and explicit approval for trades."""
    label = (
        "Acknowledge" if item["action"] in {"REVIEW", "KEEP_CASH"} else "Approve trade"
    )
    buttons = [
        [
            (label, f"rec:{item['id']}:approve"),
            ("Reject", f"rec:{item['id']}:reject"),
            ("Snooze", f"rec:{item['id']}:later"),
        ]
    ]
    if item.get("ticker"):
        buttons.append(
            [
                ("View evidence", f"rec:{item['id']}:evidence"),
                ("Review thesis", f"rec:{item['id']}:thesis"),
            ]
        )
    return buttons
