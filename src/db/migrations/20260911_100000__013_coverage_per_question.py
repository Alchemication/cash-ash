"""Relabel coverage so only the planned questions decide it."""

import json
import sqlite3

NAME = "judge coverage per planned question, ignoring extra answers"


def upgrade(conn: sqlite3.Connection) -> None:
    """Recompute every coverage label; findings and owner theses are untouched.

    The previous rule required every stored answer to be sourced, so an extra
    background remark beside fully sourced planned questions marked the whole
    assessment insufficient. Labels move in both directions here because the
    label is a function of the stored questions and answers, nothing else.
    """
    for run_id, questions_json, answers_json, coverage in conn.execute(
        "SELECT run_id, questions_json, answers_json, coverage FROM research_assessment"
    ).fetchall():
        planned = [q for q in json.loads(questions_json) if q.strip()]
        covered = {
            a.get("question", "").strip().casefold()
            for a in json.loads(answers_json)
            if a.get("kind") == "sourced" and not a.get("validation_error")
        }
        sufficient = bool(planned) and all(
            q.strip().casefold() in covered for q in planned
        )
        label = "sufficient" if sufficient else "insufficient"
        if label != coverage:
            conn.execute(
                "UPDATE research_assessment SET coverage=? WHERE run_id=?",
                (label, run_id),
            )
