"""The weekly run: everything, in order, with failures that do not stop it.

Five commands driven by hand is how a weekly habit fails to form, so this is
the one command a schedule invokes.

Stages degrade rather than abort. A failed price sync leaves yesterday's
prices and the run continues on stale data, marked stale; a failed research
pass on one holding does not stop the others; a failed decision still leaves
the report to send. The alternative — abandoning the run on the first fault —
turns a partial answer into no answer, and on a weekly cadence the next attempt
is seven days away.

Public API:
    run_weekly  -- sync, triage, research, decide, report
    StageResult -- what one stage did, and whether it worked

Example:
    from weekly import run_weekly

    outcome = run_weekly(conn, profile=profile)
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from profiles import Profile
from dataclasses import asdict, dataclass, field
from config import RESEARCH_MAX_PASSES
from datetime import date

logger = logging.getLogger(__name__)


@dataclass
class StageResult:
    """What one stage of the weekly run did."""

    name: str
    ok: bool
    detail: str
    skipped: bool = False


@dataclass
class WeeklyOutcome:
    """The whole run."""

    stages: list[StageResult] = field(default_factory=list)
    researched: list[str] = field(default_factory=list)
    recommendations: list[dict] = field(default_factory=list)
    report: str | None = None

    @property
    def ok(self) -> bool:
        """True when nothing failed outright."""
        return all(stage.ok for stage in self.stages)


def run_weekly(
    conn: sqlite3.Connection,
    *,
    profile: Profile | None = None,
    account_id: int = 1,
    today: date | None = None,
    max_research: int = RESEARCH_MAX_PASSES,
    skip_research: bool = False,
) -> WeeklyOutcome:
    """Run the full weekly cycle, continuing past stages that fail.

    Args:
        conn: Open database connection.
        profile: Profile being run for.
        account_id: Account to act on.
        today: Reference date, for tests.
        max_research: Cap on deep passes, which are the expensive stage and the
            slow one. Anything triage selected beyond this waits a week.
        skip_research: Run everything except the deep passes.

    Returns:
        What each stage did, and the report to send.
    """
    from store_workflow import start_cycle, finish_cycle
    from research_coverage import select_research

    if max_research < 0:
        raise ValueError("Research cap must be nonnegative.")
    now = today or date.today()
    cycle_id = start_cycle(conn, now.isoformat())
    outcome = WeeklyOutcome()
    outcome.stages.append(_sync(conn, account_id=account_id))

    selected = _triage(conn, outcome, account_id=account_id, today=today)

    if skip_research:
        outcome.stages.append(
            StageResult("research", ok=False, detail="skipped by request", skipped=True)
        )
    else:
        targets, deferred = select_research(
            conn, selected, today=now, limit=max_research
        )
        _research(
            conn,
            outcome,
            selected=targets,
            today=today,
            evidence_file=profile.context / "evidence.json" if profile else None,
        )
        if deferred:
            outcome.stages[-1].ok = False
            outcome.stages[-1].detail += "; deferred or manual review: " + ", ".join(
                deferred
            )

    _decide(conn, outcome, profile=profile, account_id=account_id, today=today)
    finish_cycle(conn, cycle_id, [asdict(s) for s in outcome.stages])
    _report(conn, outcome, profile=profile, account_id=account_id, today=today)
    finish_cycle(conn, cycle_id, [asdict(s) for s in outcome.stages])
    return outcome


def _sync(conn: sqlite3.Connection, *, account_id: int) -> StageResult:
    """Fetch prices, FX, dates and estimates."""
    from cmd_sync import sync_calendar, sync_prices
    from market_data import ProviderError, get_provider

    try:
        provider = get_provider()
        prices = sync_prices(conn, provider=provider, account_id=account_id)
        calendar = sync_calendar(conn, provider=provider, account_id=account_id)
    except (ProviderError, ValueError) as exc:
        # Stale prices are usable and are reported as stale; no prices is not
        # a reason to skip a week's thinking.
        return StageResult("sync", ok=False, detail=f"{exc}")
    return StageResult(
        "sync",
        ok=not prices["unpriced"],
        detail=(
            f"{len(prices['priced'])} priced, {len(prices['unpriced'])} not, "
            f"{calendar['events']} new event(s)"
        ),
    )


def _triage(
    conn: sqlite3.Connection,
    outcome: WeeklyOutcome,
    *,
    account_id: int,
    today: date | None,
) -> list[tuple[str, str]]:
    """Rank the portfolio, returning what was selected for depth."""
    from research import run_triage

    try:
        _, rankings, _ = run_triage(conn, account_id=account_id, today=today)
    except Exception as exc:  # noqa: BLE001 - the run continues without a ranking
        outcome.stages.append(StageResult("triage", ok=False, detail=f"{exc}"))
        return []
    selected = [
        (item["ticker"], item["reason"]) for item in rankings if item["selected"]
    ]
    outcome.stages.append(
        StageResult(
            "triage",
            ok=not any("omitted" in item.get("signals", ()) for item in rankings),
            detail=f"{len(selected)} of {len(rankings)} selected for depth; "
            f"{sum('omitted' in item.get('signals', ()) for item in rankings)} omitted",
        )
    )
    return selected


def _research(
    conn: sqlite3.Connection,
    outcome: WeeklyOutcome,
    *,
    selected: list[tuple[str, str]],
    today: date | None,
    evidence_file: Path | None = None,
) -> None:
    """Run a deep pass per selected holding, continuing past failures."""
    from research import research_security

    if not selected:
        outcome.stages.append(
            StageResult("research", ok=True, detail="nothing selected", skipped=True)
        )
        return

    failures: list[str] = []
    for ticker, trigger in selected:
        try:
            research_security(
                conn,
                ticker=ticker,
                trigger=trigger,
                today=today,
                evidence_file=evidence_file,
            )
            outcome.researched.append(ticker)
            assessment = conn.execute(
                "SELECT coverage FROM research_assessment WHERE security_id=(SELECT id FROM securities WHERE ticker=?) ORDER BY run_id DESC LIMIT 1",
                (ticker,),
            ).fetchone()
            if assessment and assessment["coverage"] == "insufficient":
                failures.append(ticker)
                logger.warning(
                    "Research evidence does not answer all questions for %s", ticker
                )
        except Exception as exc:  # noqa: BLE001 - one holding must not stop the rest
            logger.warning("Research failed for %s: %s", ticker, exc)
            failures.append(ticker)

    detail = f"{len(outcome.researched)} researched"
    if failures:
        detail += f", {len(failures)} failed or insufficient ({', '.join(failures)})"
    outcome.stages.append(StageResult("research", ok=not failures, detail=detail))


def _decide(
    conn: sqlite3.Connection,
    outcome: WeeklyOutcome,
    *,
    profile: Profile | None,
    account_id: int,
    today: date | None,
) -> None:
    """Propose recommendations, with the guardrails applied."""
    from decisions import run_decision

    try:
        _, recommendations, _ = run_decision(
            conn,
            profile=profile,
            account_id=account_id,
            today=today,
            blocked_reason="Weekly review incomplete; resolve failed or deferred stages before trading."
            if not outcome.ok
            else None,
        )
    except Exception as exc:  # noqa: BLE001 - the report is still worth sending
        outcome.stages.append(StageResult("decide", ok=False, detail=f"{exc}"))
        return
    outcome.recommendations = recommendations
    live = [item for item in recommendations if not item["refused"]]
    refused = len(recommendations) - len(live)
    detail = f"{len(live)} recommendation(s)"
    if refused:
        detail += f", {refused} refused by the rules"
    outcome.stages.append(StageResult("decide", ok=True, detail=detail))


def _report(
    conn: sqlite3.Connection,
    outcome: WeeklyOutcome,
    *,
    profile: Profile | None,
    account_id: int,
    today: date | None,
) -> None:
    """Render the report. Always attempted, whatever else failed."""
    from report import weekly_report

    try:
        parts = weekly_report(conn, profile=profile, account_id=account_id, today=today)
    except Exception as exc:  # noqa: BLE001
        outcome.stages.append(StageResult("report", ok=False, detail=f"{exc}"))
        return
    outcome.report = parts.body
    outcome.stages.append(
        StageResult("report", ok=True, detail=f"{len(parts.actionable)} actionable")
    )
