"""Deliver due snoozes without rerunning research or spending model tokens."""

from __future__ import annotations

from datetime import date

from notify import escape, send_with_buttons
from profiles import load_profiles
from review_text import recommendation_buttons
from store import open_existing_db
from store_workflow import now_iso


def send_due_snoozes() -> None:
    """Send each due, still-live snooze once, marking it only after delivery."""
    from config import TELEGRAM_BOT_TOKEN

    if not TELEGRAM_BOT_TOKEN:
        return
    today = date.today().isoformat()
    for profile in load_profiles().values():
        if not profile.enabled:
            continue
        with open_existing_db(profile.db) as conn:
            rows = conn.execute(
                """SELECT r.*,s.ticker,d.id AS response_id FROM recommendation r
                JOIN user_decision d ON d.id=(SELECT MAX(x.id) FROM user_decision x WHERE x.recommendation_id=r.id)
                LEFT JOIN securities s ON s.id=r.security_id
                WHERE d.decision='later' AND d.snoozed_until<=? AND d.reminder_sent_at IS NULL
                AND r.expires_on>=? AND r.superseded_by_run_id IS NULL""",
                (today, today),
            ).fetchall()
            for row in rows:
                item = dict(row)
                send_with_buttons(
                    chat_id=profile.telegram_id,
                    text=f"<b>Snoozed review is due</b> #{item['id']} {escape(item['ticker'] or '')}\n{escape(item['rationale'])}",
                    buttons=recommendation_buttons(item),
                )
                conn.execute(
                    "UPDATE user_decision SET reminder_sent_at=? WHERE id=?",
                    (now_iso(), item["response_id"]),
                )
                conn.commit()
