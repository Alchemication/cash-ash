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
        if cost
        else f"<b>{_money(total)}</b>",
    ]

    unpriced = [row for row in rows if row.value_eur is None]
    if unpriced:
        names = ", ".join(row.position.security.ticker for row in unpriced)
        lines.append(
            f"<i>{len(unpriced)} holding(s) could not be priced ({escape(names)}) "
            f"and are left out of that total.</i>"
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
        lines += [
            "",
            "<b>Nothing needs doing this week.</b>",
            "<i>That is the normal outcome, not a failure. A week where the "
            "reasons you bought things are still true is a week with no "
            "decisions in it.</i>",
        ]

    concerns = _standing_concerns(conn)
    if concerns:
        lines += ["", "<b>Standing, not new</b>"]
        lines.append(
            f"<i>{escape(', '.join(concerns))} — held without a reason you have "
            f"written down. No research will settle that; only you can.</i>"
        )

    lines += [
        "",
        f"<i>Cash {_money(cash)} · planned this month {_money(contribution)}</i>",
    ]
    return ReportParts(body="\n".join(lines).strip(), actionable=actionable)


def _pending_recommendations(conn, *, today: date) -> list[dict]:  # type: ignore[no-untyped-def]
    """Return live recommendations from the most recent run.

    Only the latest run, and only ones neither decided nor expired: a weekly
    cadence supersedes itself, so showing last week's alongside this week's
    would invite acting on research that has been replaced.
    """
    row = conn.execute(
        "SELECT MAX(run_date) AS run_date FROM recommendation"
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
            WHERE r.run_date = ?
              AND r.expires_on >= ?
              AND NOT EXISTS (
                  SELECT 1 FROM user_decision d WHERE d.recommendation_id = r.id
              )
            ORDER BY r.id
            """,
            (row["run_date"], today.isoformat()),
        )
    ]


def _standing_concerns(conn) -> list[str]:  # type: ignore[no-untyped-def]
    """Return tickers held on a thesis with no real reason behind it.

    Reported separately from the week's actions because they are not news and
    never will be. Repeating them as if they were this week's finding would be
    the generic-summary habit the whole design avoids.
    """
    return [
        row["ticker"]
        for row in conn.execute(
            """
            SELECT s.ticker
            FROM thesis t
            JOIN securities s ON s.id = t.security_id
            WHERE t.status = 'active' AND t.conviction IN ('none', 'weak')
            ORDER BY
                CASE t.conviction WHEN 'none' THEN 0 ELSE 1 END,
                s.ticker
            LIMIT 8
            """
        )
    ]
