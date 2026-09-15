"""Building the weekly portfolio message.

Written to be read on a phone by someone learning, which shapes every choice
here: short, no jargon left unexplained, and honest when the answer is that
nothing happened. A report that manufactures content to look useful trains the
reader to stop opening it.

Public API:
    weekly_report  -- render the week as Telegram HTML
    ReportParts    -- the message plus any actionable recommendations

Example:
    from report import weekly_report

    parts = weekly_report(conn, profile=profile)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
import json

from notify import escape

logger = logging.getLogger(__name__)

_ACTION_ORDER = ("EXIT", "TRIM", "BUY", "ADD", "REVIEW", "KEEP_CASH")

_ACTION_LABEL = {
    "BUY": "Buy",
    "ADD": "Add to",
    "TRIM": "Trim",
    "EXIT": "Exit",
    "REVIEW": "Needs your decision",
    "KEEP_CASH": "Keep cash",
}


@dataclass
class ReportParts:
    """A rendered weekly report.

    Attributes:
        body: The main message, in Telegram HTML.
        actionable: Recommendations that want a button, each a dict with the
            recommendation id and its rendered text.
    """

    body: str
    actionable: list[dict] = field(default_factory=list)


def _money(value: float | None) -> str:
    """Format a EUR amount for a phone screen."""
    return "—" if value is None else f"€{value:,.2f}"


def weekly_report(
    conn,  # type: ignore[no-untyped-def]
    *,
    profile=None,  # type: ignore[no-untyped-def]
    account_id: int = 1,
    today: date | None = None,
) -> ReportParts:
    """Render the week: value, what needs attention, and what does not.

    Args:
        conn: Open database connection.
        profile: Profile supplying the contribution figure.
        account_id: Account to report on.
        today: Reference date, for tests.

    Returns:
        The message and any recommendations wanting a decision.
    """
    from config import DEFAULT_MONTHLY_CONTRIBUTION_EUR
    from portfolio import cash_eur, holdings, total_value

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

    lines = [
        f"<b>Portfolio review — {now.isoformat()}</b>",
        "",
        f"<b>{_money(total)}</b>  ·  {gain:+,.2f} ({gain / cost * 100:+.1f}%) "
        f"since bought"
        if cost and not unpriced
        else f"<b>{_money(total)}</b>",
    ]

    unpriced = [row for row in rows if row.value_eur is None]
    if unpriced:
        names = ", ".join(row.position.security.ticker for row in unpriced)
        lines.append(
            f"<i>{len(unpriced)} holding(s) could not be priced ({escape(names)}) "
            f"and are left out of that total.</i>"
        )

    from quality import valuation_gaps
    from store_workflow import latest_cycle
    from config import RECOMMENDATION_EXPIRY_DAYS

    gaps = valuation_gaps(conn, rows, now)
    cycle = latest_cycle(conn)
    complete = bool(
        cycle
        and cycle["status"] == "complete"
        and 0
        <= (now - date.fromisoformat(cycle["run_date"])).days
        < RECOMMENDATION_EXPIRY_DAYS
        and not gaps
    )
    health = (
        "completed"
        if complete
        else ("not reviewed" if cycle is None else "incomplete or out of date")
    )
    lines += [
        "",
        f"<b>Review status: {health}</b>",
        f"{len(rows) - len(unpriced)}/{len(rows)} holdings priced",
    ]
    if cycle:
        lines.append(f"Last cycle: {cycle['run_date']}")
        for stage in json.loads(cycle["stages_json"]):
            lines.append(f"• {escape(stage['name'])}: {escape(stage['detail'])}")
    if gaps:
        lines.append("<i>" + escape("; ".join(gaps)) + "</i>")
    if unpriced:
        lines.append(
            "<i>Aggregate return unavailable until all holdings are priced.</i>"
        )
    pending = _pending_recommendations(conn, today=now)
    actionable: list[dict] = []

    if pending:
        lines.append("")
        for action in _ACTION_ORDER:
            group = [item for item in pending if item["action"] == action]
            if not group:
                continue
            lines.append(f"<b>{_ACTION_LABEL[action]}</b>")
            for item in group:
                ticker = f"{escape(item['ticker'])} " if item["ticker"] else ""
                amount = f"— {_money(item['amount_eur'])}" if item["amount_eur"] else ""
                lines.append(f"• {ticker}{amount}".rstrip())
                lines.append(f"  <i>{escape(item['rationale'])}</i>")
                actionable.append(item)
            lines.append("")
    else:
        lines += ["", "<b>No pending actions.</b>"]
        lines.append(
            "<i>Review completed; no action proposed.</i>"
            if complete
            else "<i>No conclusion about this week. Run main.py weekly to complete the review.</i>"
        )

    proposals = conn.execute("""SELECT t.version,t.summary,t.rationale,s.ticker FROM thesis t
        JOIN securities s ON s.id=t.security_id WHERE t.status='proposed' ORDER BY t.id DESC""").fetchall()
    if proposals:
        lines += ["", "<b>Thesis changes awaiting your review</b>"]
        for proposal in proposals:
            lines.append(
                f"• {escape(proposal['ticker'])} v{proposal['version']}: {escape(proposal['rationale'] or proposal['summary'])}"
            )
            lines.append(
                f"/thesis {escape(proposal['ticker'])} — compare and accept or reject"
            )
    approved = conn.execute("""SELECT r.id,r.action,s.ticker FROM recommendation r
        JOIN securities s ON s.id=r.security_id
        WHERE r.action IN ('BUY','ADD','TRIM','EXIT') AND
        (SELECT decision FROM user_decision d WHERE d.recommendation_id=r.id ORDER BY d.id DESC LIMIT 1)='approve'
        AND NOT EXISTS(SELECT 1 FROM execution e WHERE e.recommendation_id=r.id)""").fetchall()
    if approved:
        lines += ["", "<b>Approved; execution not recorded</b>"]
        lines += [
            f"• #{r['id']} {r['action']} {escape(r['ticker'])}: main.py executed {r['id']} --help"
            for r in approved
        ]

    latest_batch = conn.execute(
        "SELECT run_id,summary FROM decision_batch ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    if latest_batch:
        refused = conn.execute(
            "SELECT ticker,action,refusal FROM decision_refusal WHERE run_id=?",
            (latest_batch["run_id"],),
        ).fetchall()
        if refused:
            lines += ["", "<b>Trade proposals blocked</b>"]
            lines += [
                f"• {escape(r['ticker'] or '')} {r['action']}: {escape(r['refusal'])}"
                for r in refused
            ]
    concerns = _standing_concerns(
        conn,
        held={
            row.position.security.id: row.position.security.ticker
            for row in rows
            if row.position.security.id is not None
        },
    )
    if concerns:
        lines += ["", "<b>Standing, not new</b>"]
        lines += [
            f"<i>{escape(label)}: {escape(', '.join(tickers))}</i>"
            for label, tickers in concerns
        ]
        lines.append("<i>No research will settle these; only you can.</i>")

    lines += [
        "",
        f"<i>Cash {_money(cash)} · planned this month {_money(contribution)}</i>",
    ]
    return ReportParts(body="\n".join(lines).strip(), actionable=actionable)


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
        {
            "id": item["id"],
            "ticker": item["ticker"],
            "action": item["action"],
            "amount_eur": item["amount_eur"],
            "rationale": item["rationale"],
            "urgency": item["urgency"],
        }
        for item in conn.execute(
            """
            SELECT r.id, r.action, r.amount_eur, r.rationale, r.urgency, s.ticker
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
