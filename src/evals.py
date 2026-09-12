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

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

logger = logging.getLogger(__name__)

# A percentage within a few words of a term that turns it into a forecast.
# "Revenue up 12%" is a figure; "70% chance" and "confidence: 91%" may be model
# forecasts, quotations or warnings. Matches need review, not a failure label.
# "Consumer confidence fell 3%" is a figure, so a
# sentiment-index qualifier before "confidence" is let through.
_STATED_ODDS = re.compile(
    r"(\d{1,3}\s?%\s*(?:chance|probability|likelihood|likely|confidence|confident|odds)\b)"
    r"|(\b(?<!consumer )(?<!business )(?<!investor )"
    r"(?:chance|probability|likelihood|confidence|odds)\b.{0,30}?\d{1,3}\s?%)",
    re.IGNORECASE,
)


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

    checks.extend(_chat_checks(conn))

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
                "true. The bootstrap leaves the list empty rather than invent a "
                "condition, so this is a question for the owner: write what "
                "would change your mind in context/log.md and rerun "
                "'thesis bootstrap TICKER --overwrite'."
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


def _chat_checks(conn: sqlite3.Connection) -> list[Check]:
    """Return the invariants covering the chat agent's write path.

    Only integrity is checked here, not judgement. Whether an answer was a good
    answer has no threshold worth inventing; whether a write reached the ledger
    without the owner agreeing to it is a defect with no tolerable rate.
    """
    if not _has_table(conn, "pending_write"):
        return []

    applied_unconfirmed = _rows(
        conn,
        """
        SELECT id FROM pending_write
        WHERE result IS NOT NULL AND resolution IS NOT 'confirmed'
        ORDER BY id
        """,
    )
    checks = [
        Check(
            name="only a confirmed proposal was ever applied",
            passed=not applied_unconfirmed,
            why=(
                "A proposal carrying a result is one that was written to the "
                "book. If it was cancelled or expired, something applied a "
                "change the owner declined or never answered, which is the one "
                "thing the confirmation step exists to prevent."
            ),
            offenders=tuple(f"proposal {row['id']}" for row in applied_unconfirmed),
        )
    ]

    # Everything the chat path wrote carries this note, and every one of them
    # should be traceable back to a proposal the owner confirmed. One that is
    # not means a write reached the ledger around the buttons. The marker
    # differs by table so a cash flow and a trade sharing a row id cannot vouch
    # for each other.
    unbacked: list[str] = []
    for table, marker in (("cash_flows", "#{}"), ("trades", "#t{}")):
        for row in _rows(
            conn, f"SELECT id FROM {table} WHERE note LIKE '%via chat%' ORDER BY id"
        ):
            backed = _rows(
                conn,
                """
                SELECT 1 FROM pending_write
                WHERE resolution = 'confirmed' AND result LIKE ?
                """,
                (f"%[{marker.format(row['id'])}]%",),
            )
            if not backed:
                unbacked.append(f"{table} {row['id']}")
    checks.append(
        Check(
            name="every write from chat has a confirmation behind it",
            passed=not unbacked,
            why=(
                "The chat agent proposes and never writes. A cash flow or trade "
                "marked as coming from chat with no confirmed proposal behind "
                "it means that rule was bypassed, and the owner may never have "
                "seen the figure that was recorded. A trade especially: it is "
                "wrong in every derived number until somebody notices."
            ),
            offenders=tuple(unbacked),
        )
    )
    return checks


_LEDGER_FIGURE = re.compile(
    r"€\s?\d|(?<![\w.])\d{1,3}(?:\.\d+)?\s?%\s*(?:of|weight|position|portfolio)"
)
"""A figure in a note that the ledger probably already holds.

A note is replayed into later research without the conversation around it, and
a weight or a value written down today is wrong within the week — the ledger has
the real one. Numbers the owner supplied are legitimate, so this cannot be a
failure: it is a prompt to read the line and decide.
"""


def _notes_restating_figures(conn: sqlite3.Connection) -> list[str]:
    """Identify confirmed notes that look like they restate a stored figure."""
    if not _has_table(conn, "pending_write"):
        return []
    found: list[str] = []
    for row in _rows(
        conn,
        """
        SELECT id, payload_json FROM pending_write
        WHERE kind = 'context_note' AND resolution = 'confirmed'
        ORDER BY id
        """,
    ):
        line = str(json.loads(row["payload_json"]).get("line", ""))
        if _LEDGER_FIGURE.search(line):
            found.append(f"note {row['id']}")
    return found


def _stated_odds(conn: sqlite3.Connection) -> list[str]:
    """Identify possible percentage forecasts for review, without judging intent."""
    found: list[str] = []
    for row in _rows(conn, "SELECT id, rationale FROM recommendation ORDER BY id"):
        if _STATED_ODDS.search(row["rationale"]):
            found.append(f"recommendation {row['id']}")
    for row in _rows(conn, "SELECT id, rationale FROM decision_refusal ORDER BY id"):
        if _STATED_ODDS.search(row["rationale"]):
            found.append(f"refusal {row['id']}")
    for row in _rows(
        conn,
        """
        SELECT id, response_text FROM llm_call
        WHERE feature = 'chat' AND response_text IS NOT NULL
        ORDER BY id
        """,
    ):
        if _STATED_ODDS.search(row["response_text"]):
            found.append(f"chat answer {row['id']}")
    for row in _rows(
        conn,
        "SELECT run_id, reason, answers_json FROM research_assessment ORDER BY run_id",
    ):
        texts = [row["reason"]] + [
            str(a.get("answer", "")) for a in json.loads(row["answers_json"])
        ]
        if any(_STATED_ODDS.search(text) for text in texts):
            found.append(f"assessment {row['run_id']}")
    return found


def observe(
    conn: sqlite3.Connection, *, today: date | None = None, weeks: int = 8
) -> list[Observation]:
    """Return descriptive process metrics, without judging them.

    Args:
        conn: Open database connection.
        today: Reference date, for tests.
        weeks: Window for prompt activity, model calls and cost.

    Returns:
        Metrics in reading order.
    """
    now = today or date.today()
    since = (now - timedelta(weeks=weeks)).isoformat()
    out: list[Observation] = []

    stated_odds = _stated_odds(conn)
    out.append(
        Observation(
            "possible percentage forecasts to review (all history)",
            ", ".join(stated_odds) if stated_odds else "none detected",
            "Pattern matches only: quotations, negations and sourced statistics "
            "can match; other wording can be missed. Inspect these rows. "
            "This does not establish a violation or affect the eval exit status.",
        )
    )

    notes = _notes_restating_figures(conn)
    if notes:
        out.append(
            Observation(
                "notes that may restate a stored figure",
                ", ".join(notes),
                "A note is read back into later research without the "
                "conversation around it, so a weight or a value written into "
                "one is wrong by the time it is read — the ledger has the real "
                "figure. Numbers the owner supplied are fine, so read the line "
                "rather than assume. This does not affect the exit status.",
            )
        )

    out.extend(_chat_observations(conn, since=since, weeks=weeks))

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

    by_analyst = _rows(
        conn,
        """
        SELECT c.prompt_version,
               COUNT(*) n,
               SUM(CASE WHEN e.kind = 'sourced' THEN 1 ELSE 0 END) sourced
        FROM evidence e
        JOIN research_run r ON r.id = e.research_run_id
        JOIN (
            SELECT trace_id, MAX(prompt_version) prompt_version
            FROM llm_call WHERE feature = 'analyst' AND trace_id IS NOT NULL
            GROUP BY trace_id
        ) c ON c.trace_id = r.trace_id
        WHERE r.run_date BETWEEN ? AND ?
        GROUP BY c.prompt_version ORDER BY c.prompt_version
        """,
        (since, now.isoformat()),
    )
    if by_analyst:
        out.append(
            Observation(
                "claims resting on a source, by analyst prompt",
                " · ".join(
                    f"{row['prompt_version']}: {row['sourced']}/{row['n']}"
                    for row in by_analyst
                ),
                f"Last {weeks} weeks. A prompt change shows up as a different share, not as a "
                "better one; a higher share can also mean easier questions.",
            )
        )

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

    by_decide = _rows(
        conn,
        """
        WITH runs AS (
            SELECT r.id run_id, MAX(c.prompt_version) prompt_version
            FROM research_run r
            JOIN decision_batch b ON b.run_id = r.id
            JOIN llm_call c ON c.trace_id = r.trace_id AND c.feature = 'decision'
            WHERE r.run_date BETWEEN ? AND ?
            GROUP BY r.id
        ),
        proposals AS (
            SELECT research_run_id run_id, action FROM recommendation
            UNION ALL
            SELECT run_id, action FROM decision_refusal
        )
        SELECT runs.prompt_version,
               COUNT(DISTINCT runs.run_id) runs,
               SUM(CASE WHEN p.action IN ('BUY','ADD','TRIM','EXIT') THEN 1 ELSE 0 END) trades,
               SUM(CASE WHEN p.action = 'REVIEW' THEN 1 ELSE 0 END) reviews
        FROM runs LEFT JOIN proposals p ON p.run_id = runs.run_id
        GROUP BY runs.prompt_version ORDER BY runs.prompt_version
        """,
        (since, now.isoformat()),
    )
    if by_decide:
        out.append(
            Observation(
                "proposals per decision run, by decide prompt",
                " · ".join(
                    f"{row['prompt_version']}: {row['runs']} runs, "
                    f"{row['trades'] or 0} trades, {row['reviews'] or 0} REVIEW"
                    for row in by_decide
                ),
                f"Published batches in the last {weeks} weeks, including empty ones. "
                "Trades include ones the rules refused. A prompt that proposes "
                "more is behaving differently, not better or worse.",
            )
        )

    unpublished = _rows(
        conn,
        """SELECT COUNT(*) n FROM research_run r
        WHERE r.run_date BETWEEN ? AND ?
          AND EXISTS (SELECT 1 FROM llm_call c
                      WHERE c.trace_id=r.trace_id AND c.feature='decision')
          AND NOT EXISTS (SELECT 1 FROM decision_batch b WHERE b.run_id=r.id)""",
        (since, now.isoformat()),
    )[0]["n"]
    out.append(
        Observation(
            "decision runs without a published batch",
            str(unpublished),
            f"Last {weeks} weeks. Failed, rejected or still running attempts; "
            "excluded from proposal activity, never counted as decisions to do nothing.",
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


def _chat_observations(
    conn: sqlite3.Connection, *, since: str, weeks: int
) -> list[Observation]:
    """Return what the chat agent has been doing, without judging it."""
    out: list[Observation] = []

    turns = _rows(
        conn,
        """
        SELECT
            COUNT(DISTINCT t.id) turns,
            COUNT(c.id) calls,
            COALESCE(SUM(c.cost_usd), 0) cost
        FROM llm_trace t
        LEFT JOIN llm_call c ON c.trace_id = t.id
        WHERE t.operation = 'chat' AND t.started_at >= ?
        """,
        (since,),
    )[0]
    if turns["turns"]:
        per_turn = turns["calls"] / turns["turns"]
        out.append(
            Observation(
                f"chat questions in {weeks} weeks",
                f"{turns['turns']} ({turns['calls']} model calls, "
                f"{per_turn:.1f} per question)",
                "Two calls a question is the ordinary shape: one to pick a "
                "tool, one to answer. A rising figure means the loop is not "
                "converging and is worth reading in 'llm-log'.",
            )
        )
        out.append(
            Observation(
                f"chat spend in {weeks} weeks",
                f"${turns['cost']:.4f}",
                "Chat is asked for on demand rather than once a week, so it is "
                "the easiest part of the system to spend real money on. Judge "
                "it against the portfolio, not against the number.",
            )
        )

    if not _has_table(conn, "pending_write"):
        return out

    proposals = _rows(
        conn,
        """
        SELECT COALESCE(resolution, 'waiting') state, COUNT(*) n
        FROM pending_write
        WHERE proposed_at >= ?
        GROUP BY state
        ORDER BY state
        """,
        (since,),
    )
    if proposals:
        out.append(
            Observation(
                f"writes proposed in {weeks} weeks",
                ", ".join(f"{row['n']} {row['state']}" for row in proposals),
                "Cancelled means a proposal was read and refused, which is the "
                "step working. Expired means it was never answered, which more "
                "often means the sentence did not make sense than that the "
                "owner changed their mind.",
            )
        )
    return out


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    """Return whether a table exists, so a database behind a migration reports."""
    return bool(
        _rows(
            conn,
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        )
    )


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Return whether a column exists, so an older database still reports."""
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))
