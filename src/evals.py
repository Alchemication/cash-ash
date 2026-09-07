"""Measuring the process, because the outcomes cannot be measured.

Fourteen holdings a week will never produce the sample size to show that this
system picks stocks well, and replaying a past week cannot validate a model
whose training already contains what happened next. What can be measured is
whether the machinery is sound: are claims sourced, are theses falsifiable, do
the deterministic rules actually bite, and does the model finish what it starts.

The split below is deliberate. **Invariants** are things that should never be
true and are a defect when they are. **Observations** are numbers with no
correct value — reporting them as pass or fail would mean inventing a threshold
nobody can justify, and an invented threshold is worse than an honest number.

Public API:
    run_checks   -- every invariant, with what violated it
    observe      -- descriptive process metrics
    Check        -- one invariant and its result

Example:
    from evals import run_checks

    for check in run_checks(conn):
        print(check.name, check.passed)
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Check:
    """One invariant, and what broke it."""

    name: str
    passed: bool
    why: str
    offenders: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Observation:
    """A number with no correct value, reported without a verdict."""

    name: str
    value: str
    note: str = ""


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def run_checks(conn: sqlite3.Connection) -> list[Check]:
    """Return every invariant, with whatever violated it.

    Each of these is a defect rather than a preference: none has a tolerable
    rate, so none needs a threshold.

    Args:
        conn: Open database connection.

    Returns:
        One entry per invariant.
    """
    checks: list[Check] = []

    unfalsifiable = _rows(
        conn,
        """
        SELECT s.ticker FROM thesis t JOIN securities s ON s.id = t.security_id
        WHERE t.status = 'active'
          AND (t.what_would_break_it IS NULL OR t.what_would_break_it IN ('', '[]'))
        ORDER BY s.ticker
        """,
    )
    checks.append(
        Check(
            name="every active thesis can be broken",
            passed=not unfalsifiable,
            why=(
                "A thesis that states nothing which would prove it wrong cannot "
                "be tracked, so nothing downstream can notice it stopped being "
                "true."
            ),
            offenders=tuple(row["ticker"] for row in unfalsifiable),
        )
    )

    unsourced = _rows(
        conn,
        """
        SELECT id FROM evidence
        WHERE kind = 'sourced'
          AND (source_url IS NULL OR published_date IS NULL)
        """,
    )
    checks.append(
        Check(
            name="sourced claims carry a URL and a date",
            passed=not unsourced,
            why=(
                "A claim marked sourced without both is an unverifiable citation, "
                "which is worse than an honest absence."
            ),
            offenders=tuple(str(row["id"]) for row in unsourced),
        )
    )

    decided_but_retired = _rows(
        conn,
        """
        SELECT r.id FROM recommendation r
        JOIN user_decision d ON d.recommendation_id = r.id
        WHERE r.superseded_by_run_id IS NOT NULL
        """,
    )
    checks.append(
        Check(
            name="no decisions recorded on withdrawn advice",
            passed=not decided_but_retired,
            why=(
                "An answer to a recommendation a later run replaced would count "
                "as engagement with something that was never live."
            ),
            offenders=tuple(str(row["id"]) for row in decided_but_retired),
        )
    )

    adopted_by_research = _rows(
        conn,
        """
        SELECT s.ticker FROM thesis t JOIN securities s ON s.id = t.security_id
        WHERE t.status = 'active' AND t.source = 'research'
        ORDER BY s.ticker
        """,
    )
    checks.append(
        Check(
            name="research proposes and never adopts",
            passed=not adopted_by_research,
            why=(
                "An active thesis the owner never accepted means the pipeline "
                "edited the baseline it is measured against."
            ),
            offenders=tuple(row["ticker"] for row in adopted_by_research),
        )
    )

    priced_at_zero = _rows(
        conn,
        """
        SELECT ps.snapshot_date || ' ' || s.ticker AS label
        FROM snapshot_positions sp
        JOIN portfolio_snapshots ps ON ps.id = sp.snapshot_id
        JOIN securities s ON s.id = sp.security_id
        WHERE sp.value_eur = 0 AND sp.quantity > 0
        ORDER BY ps.snapshot_date, s.ticker
        """,
    )
    checks.append(
        Check(
            name="no holding recorded as worth nothing",
            passed=not priced_at_zero,
            why=(
                "A position valued at zero in the historical record understates "
                "the portfolio somewhere nobody will look again. An unpriceable "
                "holding must be absent, not zero."
            ),
            offenders=tuple(row["label"] for row in priced_at_zero),
        )
    )

    return checks


def observe(
    conn: sqlite3.Connection, *, today: date | None = None, weeks: int = 8
) -> list[Observation]:
    """Return descriptive process metrics, without judging them.

    Args:
        conn: Open database connection.
        today: Reference date, for tests.
        weeks: Window for coverage and cost.

    Returns:
        Metrics in reading order.
    """
    now = today or date.today()
    since = (now - timedelta(weeks=weeks)).isoformat()
    out: list[Observation] = []

    kinds = {
        row["kind"]: row["n"]
        for row in _rows(conn, "SELECT kind, COUNT(*) n FROM evidence GROUP BY kind")
    }
    total_claims = sum(kinds.values())
    if total_claims:
        sourced = kinds.get("sourced", 0)
        out.append(
            Observation(
                "claims resting on a source",
                f"{sourced}/{total_claims} ({sourced / total_claims * 100:.0f}%)",
                "The rest are the model's own knowledge, which is allowed but "
                "may not pose as a current fact.",
            )
        )
    else:
        out.append(Observation("claims resting on a source", "no claims recorded yet"))

    conviction = {
        row["conviction"]: row["n"]
        for row in _rows(
            conn,
            "SELECT conviction, COUNT(*) n FROM thesis WHERE status = 'active' "
            "GROUP BY conviction",
        )
    }
    if conviction:
        thin = conviction.get("none", 0) + conviction.get("weak", 0)
        out.append(
            Observation(
                "holdings held on a thin reason",
                f"{thin}/{sum(conviction.values())}",
                "Not a defect. It is the finding REVIEW exists to surface.",
            )
        )

    refusals = _rows(
        conn, "SELECT action, COUNT(*) n FROM decision_refusal GROUP BY action"
    )
    out.append(
        Observation(
            "proposals the rules refused",
            ", ".join(f"{row['action']} x{row['n']}" for row in refusals) or "none yet",
            "A model that keeps asking to sell on price alone is behaving "
            "differently from one that never does.",
        )
    )

    calls = _rows(
        conn,
        """
        SELECT COUNT(*) n,
               SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) failed,
               SUM(CASE WHEN finish_reason = 'length' THEN 1 ELSE 0 END) truncated,
               SUM(CASE WHEN model <> requested_model THEN 1 ELSE 0 END) fell_back,
               COALESCE(SUM(cost_usd), 0) cost
        FROM llm_call WHERE created_at >= ?
        """,
        (since,),
    )
    row = calls[0]
    if row["n"]:
        out.append(
            Observation(
                "model calls",
                f"{row['n']} · {row['failed']} failed · {row['truncated']} truncated "
                f"· {row['fell_back']} fell back",
                "Truncation means a budget was spent without producing an "
                "answer, and is paid for twice.",
            )
        )
        out.append(
            Observation(
                f"spend over {weeks} weeks",
                f"${row['cost']:.4f}",
                f"About ${row['cost'] / max(weeks, 1) * 52:.2f} a year at this rate.",
            )
        )

    never = _rows(
        conn,
        """
        SELECT s.ticker FROM securities s
        WHERE s.is_benchmark = 0
          AND EXISTS (SELECT 1 FROM trades t WHERE t.security_id = s.id)
          AND NOT EXISTS (
              SELECT 1 FROM evidence e WHERE e.security_id = s.id
          )
        ORDER BY s.ticker
        """,
    )
    out.append(
        Observation(
            "holdings never researched",
            f"{len(never)}"
            + (
                f" ({', '.join(never_row['ticker'] for never_row in never)})"
                if never
                else ""
            ),
            "Rotation should retire this over time; a holding that never "
            "surfaces is a gap in coverage rather than a quiet one.",
        )
    )

    decided = _rows(
        conn,
        """
        SELECT
            COUNT(*) total,
            SUM(CASE WHEN d.id IS NOT NULL THEN 1 ELSE 0 END) answered,
            SUM(CASE WHEN r.superseded_by_run_id IS NOT NULL THEN 1 ELSE 0 END) retired
        FROM recommendation r
        LEFT JOIN user_decision d ON d.recommendation_id = r.id
        WHERE r.refused_reason IS NULL
        """
        if _has_column(conn, "recommendation", "refused_reason")
        else """
        SELECT
            COUNT(*) total,
            SUM(CASE WHEN d.id IS NOT NULL THEN 1 ELSE 0 END) answered,
            SUM(CASE WHEN r.superseded_by_run_id IS NOT NULL THEN 1 ELSE 0 END) retired
        FROM recommendation r
        LEFT JOIN user_decision d ON d.recommendation_id = r.id
        """,
    )[0]
    if decided["total"]:
        out.append(
            Observation(
                "recommendations answered",
                f"{decided['answered']}/{decided['total']} "
                f"({decided['retired']} retired by a later run)",
                "Retired ones were never really live and should not count "
                "against engagement.",
            )
        )

    return out


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Return whether a column exists, so an older database still reports."""
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))
