"""Reclassify historical assessments that relied on background or missed questions."""

import json
import sqlite3

NAME = "require sourced coverage of every research question"


def upgrade(conn: sqlite3.Connection) -> None:
    """Downgrade incomplete coverage without changing findings or owner theses."""
    for run_id, questions_json, answers_json in conn.execute(
        "SELECT run_id, questions_json, answers_json FROM research_assessment "
        "WHERE coverage='sufficient'"
    ).fetchall():
        questions = json.loads(questions_json)
        answers = json.loads(answers_json)
        required = {q.strip().casefold() for q in questions if q.strip()}
        answered = {a.get("question", "").strip().casefold() for a in answers}
        sufficient = (
            bool(required)
            and required <= answered
            and bool(answers)
            and all(
                a.get("kind") == "sourced" and not a.get("validation_error")
                for a in answers
            )
        )
        if not sufficient:
            conn.execute(
                "UPDATE research_assessment SET coverage='insufficient' WHERE run_id=?",
                (run_id,),
            )
