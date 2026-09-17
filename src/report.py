"""Building the weekly portfolio message.

Written to be read on a phone by someone learning, which shapes every choice
here. The second line says whether anything needs the reader; the stage flow
fits one screen; each decision is one line, with its reasoning a tap away on
its own card rather than inline. A report that manufactures content trains the
reader to stop opening it, and one that buries the decision under its reasoning
trains them to tap buttons without reading.

Public API:
    weekly_report            -- render the week's summary as Telegram HTML
    progress_text            -- the stage flow while the weekly run is going
    card_text                -- one recommendation, as sent with its buttons
    decided_card_text        -- a card after the owner answered it
    recommendation_item      -- load one recommendation in the shape cards use
    recommendation_headline  -- the one line a recommendation is read by
    ReportParts              -- the summary plus the recommendations with cards

Example:
    from report import weekly_report

    parts = weekly_report(conn, profile=profile)
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime, timedelta

from notify import escape
from review_text import clip_line

logger = logging.getLogger(__name__)

_TRADES = ("EXIT", "TRIM", "BUY", "ADD")

_CARD_ORDER = (*_TRADES, "REVIEW")
"""Actions that get a card with buttons, most consequential first.

KEEP_CASH is not among them. It asks nothing of the owner, and a card whose only
purpose was to acknowledge doing nothing was one more thing to tap through
before reaching the ones that matter.
"""

_MOVE_LABEL = {
    "BUY": "buy",
    "ADD": "add",
    "TRIM": "trim",
    "EXIT": "exit",
    "REVIEW": "review",
}

_MARK = {"ok": "✓", "failed": "!", "skipped": "–", "running": "…", "waiting": "·"}
"""Stage marks, each single-width in Telegram's monospace font.

Emoji are not: their width varies by client, and a column of them breaks the
alignment the flow depends on.
"""

_NOT_IN_FLOW = frozenset({"report"})
"""Stages left out of the flow. The report stage renders the message itself."""

_DECIDED = {
    "reject": "✗ Rejected",
    "later": "Snoozed. It comes back when due.",
}

_ITEM_KEYS = (
    "id",
    "run_date",
    "action",
    "amount_eur",
    "rationale",
    "headline",
    "done_when",
    "urgency",
    "ticker",
)
_ITEM_COLUMNS = (
    "r.id, r.run_date, r.action, r.amount_eur, r.rationale, r.headline, r.done_when, "
    "r.urgency, s.ticker"
)


@dataclass
class ReportParts:
    """A rendered weekly review.

    Attributes:
        body: The summary message, in Telegram HTML.
        actionable: Recommendations that get a card with buttons, in card
            order, each shaped as ``recommendation_item`` returns.
    """

    body: str
    actionable: list[dict] = field(default_factory=list)


def _money(value: float | None) -> str:
    """Format a EUR amount for a phone screen."""
    return "—" if value is None else f"€{value:,.2f}"


def _day(value: date) -> str:
    """Format a date as a short day, e.g. ``Sun 13 Sep``."""
    return f"{value:%a} {value.day} {value:%b}"


def _duration(seconds: float | None) -> str:
    """Format elapsed time compactly: ``3s``, ``1m49s``, ``14m``."""
    if seconds is None:
        return ""
    whole = int(round(seconds))
    if whole < 60:
        return f"{whole}s"
    minutes, rest = divmod(whole, 60)
    return f"{minutes}m" if minutes >= 10 or not rest else f"{minutes}m{rest:02d}s"


def _plural(count: int, word: str) -> str:
    """``1 trade``, ``2 trades``."""
    return f"{count} {word}{'' if count == 1 else 's'}"


def _as_dict(stage: object) -> dict:
    """Accept a live ``StageResult`` or a stage already stored as JSON."""
    if is_dataclass(stage) and not isinstance(stage, type):
        return asdict(stage)
    return dict(stage)  # type: ignore[call-overload]


def _brief(stage: dict) -> str:
    """Return a stage's few-word outcome for the flow.

    Cycles recorded before stages carried a brief have only the long detail;
    its first clause stands in.
    """
    from config import REPORT_STAGE_BRIEF_MAX_CHARS

    if stage.get("brief"):
        return str(stage["brief"])
    first = str(stage.get("detail", "")).split(";")[0]
    return clip_line(first, REPORT_STAGE_BRIEF_MAX_CHARS)


def _stage_mark(stage: dict) -> str:
    """Mark a finished stage as ok, failed or skipped."""
    if stage.get("skipped"):
        return _MARK["skipped"]
    return _MARK["ok"] if stage.get("ok") else _MARK["failed"]


def _flow_line(name: str, mark: str, seconds: float | None, text: str) -> str:
    """One aligned row: mark, stage, time, outcome."""
    return f"{mark} {name:<8} {_duration(seconds):>5}  {text}".rstrip()


def _flow(
    stages: list[dict],
    *,
    running: str | None = None,
    note: str | None = None,
    waiting: tuple[str, ...] = (),
    total: tuple[float | None, int, float] | None = None,
) -> str:
    """Render stages as an aligned monospace block.

    Args:
        stages: Finished stages.
        running: Stage in progress, if any.
        note: What the running stage is on, e.g. ``2/4 TEST``.
        waiting: Stages not started yet.
        total: ``(seconds, model calls, cost in USD)`` for a finished cycle.

    Returns:
        A ``<pre>`` block.
    """
    lines = [
        _flow_line(
            stage["name"], _stage_mark(stage), stage.get("seconds"), _brief(stage)
        )
        for stage in stages
        if stage["name"] not in _NOT_IN_FLOW
    ]
    if running is not None:
        lines.append(_flow_line(running, _MARK["running"], None, note or "running"))
    lines += [_flow_line(name, _MARK["waiting"], None, "") for name in waiting]
    if total is not None:
        seconds, calls, cost = total
        spend = f"{_plural(calls, 'call')} · ${cost:.2f}" if calls else "no model calls"
        lines.append(_flow_line("total", " ", seconds, spend))
    return "<pre>" + escape("\n".join(lines)) + "</pre>"


def progress_text(
    stages: list,
    running: str | None,
    note: str | None = None,
    *,
    run_date: date,
) -> str:
    """Render the stage flow while the weekly run is still going.

    Args:
        stages: Stages finished so far, as ``StageResult`` objects or dicts.
        running: Stage now starting, or None.
        note: What the running stage is on, if it says.
        run_date: Date the run is for.

    Returns:
        Telegram HTML.
    """
    from weekly import STAGE_ORDER

    done = [_as_dict(stage) for stage in stages]
    started = {stage["name"] for stage in done} | {running}
    waiting = tuple(
        name for name in STAGE_ORDER if name not in started and name not in _NOT_IN_FLOW
    )
    shown = running if running not in _NOT_IN_FLOW else None
    return (
        f"<b>Weekly review · {_day(run_date)}</b>\n"
        f"<i>Running. The summary follows when it is done.</i>\n\n"
        + _flow(done, running=shown, note=note, waiting=waiting)
    )


def recommendation_headline(item: dict) -> str:
    """Return the one line a recommendation is read by.

    Recommendations made before headlines existed fall back to the first
    sentence of their rationale, cut to the same budget.

    Args:
        item: A recommendation as returned by ``recommendation_item``.

    Returns:
        Plain text, not yet escaped.
    """
    from config import RECOMMENDATION_HEADLINE_MAX_CHARS

    if item.get("headline"):
        return str(item["headline"])
    rationale = str(item.get("rationale") or "").strip()
    first = re.split(r"(?<=[.!?])\s+", rationale, maxsplit=1)[0]
    return (
        clip_line(first, RECOMMENDATION_HEADLINE_MAX_CHARS) or "No explanation given."
    )


def card_text(item: dict) -> str:
    """Render one recommendation as sent with its buttons.

    The headline and, for a review, what settles it are shown. The rationale
    sits collapsed underneath, opened only by someone who wants the reasoning,
    and is cut to fit: a message carrying buttons cannot be split.

    Args:
        item: A recommendation as returned by ``recommendation_item``.

    Returns:
        Telegram HTML, leaving room for ``decided_card_text`` to append.
    """
    from config import TELEGRAM_MAX_MESSAGE_CHARS

    amount = _money(item["amount_eur"]) if item.get("amount_eur") else None
    title = " · ".join(
        part for part in (item["action"], item.get("ticker"), amount) if part
    )
    lines = [f"<b>{escape(title)}</b>"]
    # A headline derived from the rationale would repeat its first sentence
    # directly above it, so only a stored one gets its own line.
    if item.get("headline"):
        lines.append(escape(str(item["headline"])))
    if item.get("done_when"):
        lines.append(f"<i>Done when:</i> {escape(item['done_when'])}")
    if item["action"] in _TRADES and item.get("run_date"):
        import config

        wait = (
            config.BUY_COOLING_OFF_DAYS
            if item["action"] in {"BUY", "ADD"}
            else config.SELL_COOLING_OFF_DAYS
        )
        ready = date.fromisoformat(item["run_date"]) + timedelta(days=wait)
        lines.append(f"<i>Approve from {_day(ready)}, if you still agree.</i>")
    why = str(item.get("rationale") or "").strip()
    if why:
        wrapper = "\n<blockquote expandable></blockquote>"
        budget = TELEGRAM_MAX_MESSAGE_CHARS - len("\n".join(lines)) - len(wrapper) - 120
        limit = budget
        body = escape(" ".join(why.split()))
        while len(body) > budget and limit > 0:
            body = escape(clip_line(why, limit))
            limit -= 200
        lines.append(f"<blockquote expandable>{body}</blockquote>")
    return "\n".join(lines)


def decided_card_text(item: dict, decision: str) -> str:
    """Render a card after the owner answered it.

    Sent as an edit without buttons, so the chat itself shows which cards are
    still open.

    Args:
        item: A recommendation as returned by ``recommendation_item``.
        decision: ``approve``, ``reject`` or ``later``.

    Returns:
        Telegram HTML.
    """
    if decision == "approve":
        outcome = (
            "✓ Done"
            if item["action"] in {"REVIEW", "KEEP_CASH"}
            else f"✓ Approved. Record the fill: main.py executed {item['id']}"
        )
    else:
        outcome = _DECIDED[decision]
    return f"{card_text(item)}\n\n<b>{escape(outcome)}</b>"


def recommendation_item(
    conn: sqlite3.Connection, recommendation_id: int
) -> dict | None:
    """Load one recommendation in the shape cards use.

    Args:
        conn: Open database connection.
        recommendation_id: Recommendation to load.

    Returns:
        The recommendation, or None if there is no such row.
    """
    row = conn.execute(
        f"SELECT {_ITEM_COLUMNS} FROM recommendation r "
        "LEFT JOIN securities s ON s.id = r.security_id WHERE r.id = ?",
        (recommendation_id,),
    ).fetchone()
    return dict(zip(_ITEM_KEYS, row)) if row is not None else None


def weekly_report(
    conn,  # type: ignore[no-untyped-def]
    *,
    profile=None,  # type: ignore[no-untyped-def]
    account_id: int = 1,
    today: date | None = None,
) -> ReportParts:
    """Render the week: whether anything needs the owner, how the run went.

    Args:
        conn: Open database connection.
        profile: Profile supplying the contribution figure.
        account_id: Account to report on.
        today: Reference date, for tests.

    Returns:
        The summary and the recommendations that get a card.
    """
    from config import DEFAULT_MONTHLY_CONTRIBUTION_EUR, RECOMMENDATION_EXPIRY_DAYS
    from portfolio import cash_eur, holdings, total_value
    from quality import valuation_gaps
    from store_workflow import latest_cycle

    now = today or date.today()
    rows = holdings(conn, account_id=account_id)
    cash = cash_eur(conn, account_id=account_id)
    total = total_value(rows, cash=cash)
    unpriced = [row for row in rows if row.value_eur is None]
    cost = sum(row.position.cost_basis_eur for row in rows)
    gain = total - cash - cost
    contribution = (
        profile.monthly_contribution_eur
        if profile is not None
        else DEFAULT_MONTHLY_CONTRIBUTION_EUR
    )

    gaps = valuation_gaps(conn, rows, now)
    cycle = latest_cycle(conn)
    run_date = date.fromisoformat(cycle["run_date"]) if cycle else None
    stages = json.loads(cycle["stages_json"]) if cycle else []
    fresh = bool(
        run_date is not None and 0 <= (now - run_date).days < RECOMMENDATION_EXPIRY_DAYS
    )
    complete = bool(fresh and cycle["status"] == "complete" and not gaps)

    pending = _pending_recommendations(conn, today=now)
    cards = sorted(
        (item for item in pending if item["action"] in _CARD_ORDER),
        key=lambda item: _CARD_ORDER.index(item["action"]),
    )
    keep_cash = next((item for item in pending if item["action"] == "KEEP_CASH"), None)
    unfilled = conn.execute("""SELECT r.id,r.action,s.ticker FROM recommendation r
        JOIN securities s ON s.id=r.security_id
        WHERE r.action IN ('BUY','ADD','TRIM','EXIT') AND
        (SELECT decision FROM user_decision d WHERE d.recommendation_id=r.id ORDER BY d.id DESC LIMIT 1)='approve'
        AND NOT EXISTS(SELECT 1 FROM execution e WHERE e.recommendation_id=r.id)""").fetchall()
    waiting_theses = [
        row[0]
        for row in conn.execute(
            """SELECT DISTINCT s.ticker FROM thesis t
            JOIN securities s ON s.id = t.security_id
            WHERE t.status = 'proposed' ORDER BY s.ticker"""
        )
    ]

    title = (
        f"Weekly review · {_day(run_date)}" if run_date else f"Portfolio · {_day(now)}"
    )
    verdict = _verdict(
        cards, len(unfilled), reviewed=cycle is not None, fresh=fresh, complete=complete
    )
    lines = [f"<b>{escape(title)}</b>", f"<b>{escape(verdict)}</b>", ""]
    lines.append(
        f"<b>{_money(total)}</b> · {gain:+,.2f} ({gain / cost * 100:+.1f}%) "
        f"since bought"
        if cost and not unpriced
        else f"<b>{_money(total)}</b>"
    )
    if run_date is not None and run_date != now:
        lines.append(f"<i>Valued {_day(now)}; the review ran {_day(run_date)}.</i>")
    if unpriced:
        names = ", ".join(row.position.security.ticker for row in unpriced)
        lines.append(
            f"<i>{len(unpriced)} holding(s) could not be priced ({escape(names)}) "
            f"and are left out of that total. Aggregate return unavailable until "
            f"all holdings are priced.</i>"
        )
    if gaps:
        lines.append("<i>" + escape("; ".join(gaps)) + "</i>")

    from insights import portfolio_picture

    picture = portfolio_picture(conn, account_id=account_id, today=now)
    if picture:
        lines += [
            "",
            "<b>Your portfolio</b>",
            *(f"• {escape(fact)}" for fact in picture),
        ]

    if cycle is not None and run_date is not None:
        lines += ["", _flow(stages, total=(_elapsed(cycle), *_spend(conn, cycle)))]
        if not fresh:
            lines.append(
                f"<i>This review is {(now - run_date).days} days old. "
                f"Run main.py weekly for a current one.</i>"
            )
        elif cycle["status"] != "complete":
            lines.append(
                "<i>The review did not complete, so trades are blocked this "
                "week. Details below.</i>"
            )

    moves = [_move_line(number, item) for number, item in enumerate(cards, 1)]
    moves += [
        f"Record the fill: #{r['id']} {r['action']} {escape(r['ticker'])} · "
        f"main.py executed {r['id']}"
        for r in unfilled
    ]
    if waiting_theses:
        moves.append(
            f"Thesis changes to review: {escape(', '.join(waiting_theses))} · "
            f"/thesis TICKER"
        )
    if moves:
        lines += ["", "<b>Your move</b>", *moves]
    if keep_cash is not None:
        lines += [
            "",
            f"<i>Keep cash: {escape(recommendation_headline(keep_cash))}</i>",
        ]

    from record import record_lines, shadow_record

    followed = record_lines(shadow_record(conn, account_id=account_id, today=now))
    if followed:
        lines += [
            "",
            "<b>If you had followed it</b>",
            *(f"• {escape(line)}" for line in followed),
        ]

    held = {
        row.position.security.id: row.position.security.ticker
        for row in rows
        if row.position.security.id is not None
    }
    details = _details(conn, stages, held=held)
    if details:
        lines += [
            "",
            "<blockquote expandable><b>Details</b>\n"
            + "\n".join(details)
            + "</blockquote>",
        ]

    lines += [
        "",
        f"<i>Cash {_money(cash)} · planned this month {_money(contribution)}</i>",
    ]
    return ReportParts(body="\n".join(lines).strip(), actionable=cards)


def _verdict(
    cards: list[dict], unfilled: int, *, reviewed: bool, fresh: bool, complete: bool
) -> str:
    """Say in one line whether anything needs the owner."""
    trades = sum(item["action"] in _TRADES for item in cards)
    reviews = len(cards) - trades
    parts: list[str] = []
    if trades:
        parts.append(f"{_plural(trades, 'trade')} to approve")
    if reviews:
        parts.append(f"{reviews} {'needs' if reviews == 1 else 'need'} your input")
    if unfilled:
        parts.append(f"{_plural(unfilled, 'fill')} to record")
    if parts:
        if reviews and not trades:
            parts.append("no trades")
        return "▶ " + " · ".join(parts)
    if not reviewed:
        return "Not reviewed yet. Run main.py weekly."
    if not fresh:
        return "! Out of date. Run main.py weekly."
    if not complete:
        return "! No conclusion: the review did not complete."
    return "✓ Nothing needs you this week."


def _move_line(number: int, item: dict) -> str:
    """One numbered decision: who, what, and the headline."""
    who = escape(item["ticker"]) if item.get("ticker") else "Portfolio"
    label = _MOVE_LABEL[item["action"]]
    if item.get("amount_eur"):
        label += f" {_money(item['amount_eur'])}"
    return f"{number}. <b>{who}</b> {label} — {escape(recommendation_headline(item))}"


def _elapsed(cycle: dict) -> float | None:
    """Wall-clock seconds from the cycle's start to its last recorded stage."""
    if not cycle.get("finished_at"):
        return None
    return (
        datetime.fromisoformat(cycle["finished_at"])
        - datetime.fromisoformat(cycle["started_at"])
    ).total_seconds()


def _spend(conn: sqlite3.Connection, cycle: dict) -> tuple[int, float]:
    """Model calls and their cost logged while the cycle ran, chat excluded.

    Counted by timestamp so that cycles recorded before stages carried their
    own counts still report a total.
    """
    row = conn.execute(
        """SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM llm_call
        WHERE feature != 'chat' AND created_at >= ? AND created_at <= ?""",
        (cycle["started_at"], cycle.get("finished_at") or "9999"),
    ).fetchone()
    return int(row[0]), float(row[1])


def _details(
    conn: sqlite3.Connection, stages: list[dict], *, held: dict[int, str]
) -> list[str]:
    """What the collapsed section holds: failures, refusals, standing concerns."""
    lines = [
        f"{escape(stage['name'])}: {escape(stage['detail'])}"
        for stage in stages
        if not stage.get("ok")
    ]
    latest_batch = conn.execute(
        "SELECT run_id FROM decision_batch ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    if latest_batch:
        refused = conn.execute(
            "SELECT ticker,action,refusal FROM decision_refusal WHERE run_id=?",
            (latest_batch[0],),
        ).fetchall()
        if refused:
            lines += ["", "<b>Blocked by the rules</b>"]
            lines += [
                f"{escape(r[0] or '')} {r[1]}: {escape(r[2])}".strip() for r in refused
            ]
    concerns = _standing_concerns(conn, held=held)
    if concerns:
        lines += ["", "<b>Standing, not new</b>"]
        lines += [
            f"{escape(label)}: {escape(', '.join(tickers))}"
            for label, tickers in concerns
        ]
        lines.append("<i>No research will settle these; only you can.</i>")
    while lines and not lines[0]:
        lines.pop(0)
    return lines


def _pending_recommendations(conn, *, today: date) -> list[dict]:  # type: ignore[no-untyped-def]
    """Return live recommendations from the most recent run.

    Only live ones: not decided, not expired, and not retired by a later run.
    A weekly cadence supersedes itself, so showing last week's beside this
    week's would invite acting on research already replaced — and re-running
    after a failed stage would otherwise send the same recommendation twice.
    """
    row = conn.execute(
        """
        SELECT MAX(run_date) AS run_date FROM recommendation
        WHERE superseded_by_run_id IS NULL
        """
    ).fetchone()
    if row is None or row["run_date"] is None:
        return []
    return [
        dict(zip(_ITEM_KEYS, item))
        for item in conn.execute(
            f"""
            SELECT {_ITEM_COLUMNS}
            FROM recommendation r
            LEFT JOIN securities s ON s.id = r.security_id
            WHERE r.superseded_by_run_id IS NULL
              AND r.expires_on >= ?
              AND COALESCE((SELECT decision FROM user_decision d WHERE d.recommendation_id=r.id ORDER BY d.id DESC LIMIT 1), '') NOT IN ('approve','reject')
              AND COALESCE((SELECT snoozed_until FROM user_decision d WHERE d.recommendation_id=r.id ORDER BY d.id DESC LIMIT 1), '') <= ?
            ORDER BY r.id
            """,
            (today.isoformat(), today.isoformat()),
        )
    ]


_CONCERN_LABELS: tuple[tuple[str, str], ...] = (
    ("missing", "No thesis recorded"),
    ("none", "No reason beyond wanting to own it"),
    ("weak", "A reason, but not tied to the business or its price"),
)


def _standing_concerns(
    conn,  # type: ignore[no-untyped-def]
    *,
    held: dict[int, str],
) -> list[tuple[str, list[str]]]:
    """Group current holdings whose reason is missing or weak.

    Reported separately from the week's actions because they are not news and
    never will be. Repeating them as if they were this week's finding would be
    the generic-summary habit the whole design avoids. A weak reason and an
    absent one are different findings, so each gets its own line.

    Args:
        conn: Open database connection.
        held: Ticker by security id for every current holding.

    Returns:
        ``(label, tickers)`` per non-empty group, most severe first.
    """
    conviction = {
        row["security_id"]: row["conviction"]
        for row in conn.execute(
            "SELECT security_id, conviction FROM thesis WHERE status = 'active'"
        )
    }
    groups = {
        key: sorted(
            ticker
            for security_id, ticker in held.items()
            if conviction.get(security_id, "missing") == key
        )
        for key, _ in _CONCERN_LABELS
    }
    return [(label, groups[key]) for key, label in _CONCERN_LABELS if groups[key]]
