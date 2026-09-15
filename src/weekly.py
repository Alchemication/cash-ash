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
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar
from profiles import Profile
from dataclasses import asdict, dataclass, field
from config import RESEARCH_MAX_PASSES
from datetime import date

logger = logging.getLogger(__name__)

T = TypeVar("T")

STAGE_ORDER: tuple[str, ...] = (
    "sync",
    "snapshot",
    "triage",
    "research",
    "decide",
    "report",
)
"""Stages in the order a run performs them, so a watcher can see what is left."""

Progress = Callable[[list, str | None, str | None], None]
"""Told ``(finished stages, stage starting, what it is on)`` as a run goes."""


@dataclass
class StageResult:
    """What one stage of the weekly run did.

    Attributes:
        name: Stage name, one of ``STAGE_ORDER``.
        ok: False when the stage failed or left its work incomplete.
        detail: The full outcome, for the CLI table and the report's details.
        skipped: True when the stage deliberately did nothing.
        brief: A few words for the stage flow on a phone.
        seconds: Wall-clock time the stage took.
        model_calls: Model calls logged while it ran, chat excluded.
        cost_usd: What those calls cost.
    """

    name: str
    ok: bool
    detail: str
    skipped: bool = False
    brief: str = ""
    seconds: float | None = None
    model_calls: int = 0
    cost_usd: float = 0.0


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
    progress: Progress | None = None,
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
        progress: Told as each stage starts, and as research reaches each
            holding. A failing watcher is logged and never stops the run.

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

    def stage(name: str, step: Callable[[], T]) -> T:
        return _timed(conn, outcome, name, step, progress)

    stage("sync", lambda: _sync(conn, account_id=account_id))
    stage("snapshot", lambda: _snapshot(conn, account_id=account_id, today=today))
    selected = stage(
        "triage", lambda: _triage(conn, outcome, account_id=account_id, today=today)
    )

    if skip_research:
        outcome.stages.append(
            StageResult(
                "research",
                ok=False,
                detail="skipped by request",
                skipped=True,
                brief="skipped by request",
            )
        )
    else:
        targets, deferred = select_research(
            conn, selected, today=now, limit=max_research
        )
        stage(
            "research",
            lambda: _research(
                conn,
                outcome,
                selected=targets,
                today=today,
                evidence_file=profile.context / "evidence.json" if profile else None,
                progress=progress,
            ),
        )
        if deferred:
            outcome.stages[-1].ok = False
            outcome.stages[-1].detail += "; deferred or manual review: " + ", ".join(
                deferred
            )
            outcome.stages[-1].brief += f", {len(deferred)} deferred"

    stage(
        "decide",
        lambda: _decide(
            conn, outcome, profile=profile, account_id=account_id, today=today
        ),
    )
    finish_cycle(conn, cycle_id, [asdict(s) for s in outcome.stages])
    stage(
        "report",
        lambda: _report(
            conn, outcome, profile=profile, account_id=account_id, today=today
        ),
    )
    finish_cycle(conn, cycle_id, [asdict(s) for s in outcome.stages])
    return outcome


def _notify_progress(
    progress: Progress | None,
    stages: list[StageResult],
    running: str | None,
    note: str | None = None,
) -> None:
    """Tell a watcher what is starting. A failed update never stops the run."""
    if progress is None:
        return
    try:
        progress(list(stages), running, note)
    except Exception:  # noqa: BLE001 - progress is a courtesy; the run is the point
        logger.warning("Progress update failed", exc_info=True)


def _timed(
    conn: sqlite3.Connection,
    outcome: WeeklyOutcome,
    name: str,
    step: Callable[[], T],
    progress: Progress | None,
) -> T:
    """Run one stage, stamping its duration and the model calls it made.

    Calls are attributed by timestamp rather than threaded through every stage:
    each is logged as it completes, so those logged after this stage started
    are its own. Chat is excluded, since the listener can answer a question
    while the run is going.

    Args:
        conn: Open database connection.
        outcome: The run so far. The stage appends to it or returns its result.
        name: Stage name, announced as it starts.
        step: The stage itself.
        progress: Watcher to tell, if any.

    Returns:
        Whatever the stage returned.
    """
    from store_workflow import now_iso

    _notify_progress(progress, outcome.stages, name)
    started_at = now_iso()
    started = time.monotonic()
    before = len(outcome.stages)
    result = step()
    if isinstance(result, StageResult):
        outcome.stages.append(result)
    if len(outcome.stages) > before:
        finished = outcome.stages[-1]
        finished.seconds = round(time.monotonic() - started, 1)
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM llm_call "
            "WHERE feature != 'chat' AND created_at >= ?",
            (started_at,),
        ).fetchone()
        finished.model_calls = int(row[0])
        finished.cost_usd = float(row[1])
    return result


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
        return StageResult(
            "sync", ok=False, detail=f"{exc}", brief="failed; last prices used"
        )
    priced = len(prices["priced"])
    return StageResult(
        "sync",
        ok=not prices["unpriced"],
        detail=(
            f"{priced} priced, {len(prices['unpriced'])} not, "
            f"{calendar['events']} new event(s)"
        ),
        brief=(
            f"{priced}/{priced + len(prices['unpriced'])} priced, "
            f"{calendar['events']} events"
        ),
    )


def _snapshot(conn, *, account_id: int, today) -> StageResult:  # type: ignore[no-untyped-def]
    """Record what the portfolio was worth, right after prices were refreshed.

    Value over time is derivable from dated trades and dated prices, so this is
    not the only record — but a derived figure silently changes when a price is
    later corrected, while a snapshot pins what was actually reported at the
    time. It is also what turns "when did this diverge from the index" into a
    query rather than a reconstruction.

    Unpriced holdings are omitted rather than recorded as zero, and the count
    goes in the note: a snapshot that quietly valued a suspended holding at
    nothing would understate the portfolio for as long as the outage lasted,
    and would do it in the historical record where nobody would look again.

    Args:
        conn: Open database connection.
        account_id: Account to snapshot.
        today: Reference date, for tests.

    Returns:
        What the stage did.
    """
    from datetime import date as _date

    from portfolio import cash_eur, holdings
    from store import save_snapshot

    when = (today or _date.today()).isoformat()
    try:
        rows = holdings(conn, account_id=account_id)
        priced = [row for row in rows if row.value_eur is not None]
        unpriced = len(rows) - len(priced)
        save_snapshot(
            conn,
            account_id=account_id,
            snapshot_date=when,
            cash_eur=cash_eur(conn, account_id=account_id),
            positions=[
                (
                    row.position.security.id,
                    row.position.quantity,
                    row.value_eur,
                    row.unrealised_return_pct,
                )
                for row in priced
                if row.position.security.id is not None
            ],
            source="weekly",
            note=(
                f"Weekly snapshot. {unpriced} holding(s) omitted for want of a price."
                if unpriced
                else "Weekly snapshot."
            ),
        )
    except Exception as exc:  # noqa: BLE001 - the run continues without a record
        return StageResult("snapshot", ok=False, detail=f"{exc}", brief="not recorded")

    detail = f"{len(priced)} holding(s) valued"
    if unpriced:
        detail += f", {unpriced} unpriced and omitted"
    return StageResult(
        "snapshot", ok=True, detail=detail, brief=f"{len(priced)} valued"
    )


def _triage(
    conn: sqlite3.Connection,
    outcome: WeeklyOutcome,
    *,
    account_id: int,
    today: date | None,
) -> list[tuple[str, str]]:
    """Rank the portfolio, returning what was selected for depth."""
    from triage import run_triage

    try:
        _, rankings, _ = run_triage(conn, account_id=account_id, today=today)
    except Exception as exc:  # noqa: BLE001 - the run continues without a ranking
        outcome.stages.append(
            StageResult("triage", ok=False, detail=f"{exc}", brief="failed")
        )
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
            brief=f"{len(selected)} of {len(rankings)} flagged",
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
    progress: Progress | None = None,
) -> None:
    """Run a deep pass per selected holding, continuing past failures."""
    from research import research_security

    if not selected:
        outcome.stages.append(
            StageResult(
                "research",
                ok=True,
                detail="nothing selected",
                skipped=True,
                brief="nothing flagged",
            )
        )
        return

    failures: list[str] = []
    errors = 0
    for index, (ticker, trigger) in enumerate(selected, 1):
        _notify_progress(
            progress, outcome.stages, "research", f"{index}/{len(selected)} {ticker}"
        )
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
            errors += 1

    detail = f"{len(outcome.researched)} researched"
    if failures:
        detail += f", {len(failures)} failed or insufficient ({', '.join(failures)})"
    conclusive = len(outcome.researched) - (len(failures) - errors)
    brief = f"{len(outcome.researched)} run, {conclusive} conclusive"
    if errors:
        brief += f", {errors} failed"
    outcome.stages.append(
        StageResult("research", ok=not failures, detail=detail, brief=brief)
    )


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
        outcome.stages.append(
            StageResult("decide", ok=False, detail=f"{exc}", brief="failed")
        )
        return
    outcome.recommendations = recommendations
    live = [item for item in recommendations if not item["refused"]]
    refused = len(recommendations) - len(live)
    detail = f"{len(live)} recommendation(s)"
    if refused:
        detail += f", {refused} refused by the rules"
    counts: dict[str, int] = {}
    for item in live:
        counts[item["action"]] = counts.get(item["action"], 0) + 1
    brief = (
        ", ".join(
            "keep cash" if action == "KEEP_CASH" else f"{count} {action.lower()}"
            for action, count in counts.items()
        )
        or "no action"
    )
    if refused:
        brief += f", {refused} blocked"
    outcome.stages.append(StageResult("decide", ok=True, detail=detail, brief=brief))


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
        outcome.stages.append(
            StageResult("report", ok=False, detail=f"{exc}", brief="failed")
        )
        return
    outcome.report = parts.body
    outcome.stages.append(
        StageResult(
            "report",
            ok=True,
            detail=f"{len(parts.actionable)} actionable",
            brief=f"{len(parts.actionable)} cards",
        )
    )
