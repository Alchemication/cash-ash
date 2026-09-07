"""Commands for funded cash, manual execution and process measurements."""

from __future__ import annotations

import argparse
import json
import math
from datetime import date

from profiles import resolve_cli_profile
from store import open_existing_db


def cmd_executed(args: argparse.Namespace) -> None:
    """Record a broker fill, or explicitly close an approved action without trading."""
    from workflow import record_execution

    _, path = resolve_cli_profile(args.profile, db=args.db)
    with open_existing_db(path) as conn:
        record_execution(
            conn,
            args.recommendation_id,
            quantity=args.quantity,
            amount=args.amount,
            fee=args.fee,
            day=args.date,
            note=args.note,
            skipped=args.not_executed,
        )
    print(
        "Recorded: not executed."
        if args.not_executed
        else "Execution and trade recorded. Holdings now derive from the updated ledger."
    )


def cmd_cash(args: argparse.Namespace) -> None:
    """Record actual contributions so planned funding is never spendable by accident."""
    from models import CashFlow
    from store import insert_cash_flow

    if not math.isfinite(args.amount) or args.amount <= 0:
        raise ValueError("Enter a positive, finite EUR contribution.")
    day = date.fromisoformat(args.date) if args.date else date.today()
    if day > date.today():
        raise ValueError("Record contributions only after the cash has arrived.")
    _, path = resolve_cli_profile(args.profile, db=args.db)
    with open_existing_db(path) as conn:
        insert_cash_flow(
            conn,
            CashFlow(
                flow_date=day.isoformat(),
                kind="CONTRIBUTION",
                amount_eur=args.amount,
                note=args.note,
            ),
        )
    print("Contribution recorded. This cash is now funded.")


def cmd_process(args: argparse.Namespace) -> None:
    """Print observed process metrics, without treating outcomes as model skill."""
    from config import (
        EVIDENCE_ITEMS_PER_SECURITY,
        EVIDENCE_MAX_AGE_DAYS,
        RESEARCH_MAX_PASSES,
        RESEARCH_ROTATION_SLOTS,
        RESEARCH_ASSET_CLASSES,
        RESEARCH_OVERDUE_DAYS,
        SNOOZE_DAYS,
    )
    from store_workflow import latest_assessments

    _, path = resolve_cli_profile(args.profile, db=args.db)
    with open_existing_db(path) as conn:
        assessments = latest_assessments(conn)
        answers = [a for row in assessments for a in json.loads(row["answers_json"])]
        cost = conn.execute(
            "SELECT SUM(cost_usd),COUNT(*),SUM(cost_usd IS NULL) FROM llm_call"
        ).fetchone()
        rejected = sum(bool(a.get("validation_error")) for a in answers)
        reversals = conn.execute("""SELECT COUNT(*) FROM user_decision d WHERE d.decision IN ('approve','reject') AND
            EXISTS(SELECT 1 FROM user_decision p WHERE p.recommendation_id=d.recommendation_id
                   AND p.id<d.id AND p.decision IN ('approve','reject') AND p.decision<>d.decision)""").fetchone()[
            0
        ]
        print(
            json.dumps(
                {
                    "latest_assessments": len(assessments),
                    "questions_unanswered": sum(
                        a["kind"] == "unanswered" for a in answers
                    ),
                    "claims_with_verified_citation": sum(
                        a["kind"] == "sourced" for a in answers
                    ),
                    "citations_rejected": rejected,
                    "decision_reversals": reversals,
                    "model_calls": cost[1],
                    "known_cost_usd": cost[0],
                    "calls_without_cost": cost[2] or 0,
                    "limits": {
                        "RESEARCH_MAX_PASSES": RESEARCH_MAX_PASSES,
                        "RESEARCH_ROTATION_SLOTS": RESEARCH_ROTATION_SLOTS,
                        "RESEARCH_ASSET_CLASSES": RESEARCH_ASSET_CLASSES,
                        "RESEARCH_OVERDUE_DAYS": RESEARCH_OVERDUE_DAYS,
                        "SNOOZE_DAYS": SNOOZE_DAYS,
                        "EVIDENCE_ITEMS_PER_SECURITY": EVIDENCE_ITEMS_PER_SECURITY,
                        "EVIDENCE_MAX_AGE_DAYS": EVIDENCE_MAX_AGE_DAYS,
                    },
                    "interpretation": "Citation checks verify supplied IDs and excerpts, not semantic support or investment skill.",
                },
                indent=2,
            )
        )
