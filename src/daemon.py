"""Long-polling listener: receives button presses and chat commands.

Sending a report needs no daemon. Receiving a reply does, because Telegram
delivers updates by being asked for them, and something has to keep asking.

Every update is routed by the sender's numeric Telegram id to a profile in the
roster. An update from an id that is not in the roster is ignored in silence —
not answered, not acknowledged — because the bot's username is discoverable and
a stranger who finds it must not be able to read anyone's portfolio or learn
that the account exists.

Public API:
    run_daemon    -- poll until stopped
    handle_update -- route one update; the unit the tests exercise

Example:
    from daemon import run_daemon

    run_daemon()
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime

from config import APP_HOME, DAEMON_ERROR_BACKOFF_S, DAEMON_POLL_TIMEOUT_S
from notify import TelegramError, answer_callback, get_updates, send_message

logger = logging.getLogger(__name__)

STATE_PATH = APP_HOME / "daemon_state.json"

_CALLBACK = re.compile(r"^rec:(\d+):(approve|reject|later)$")
"""Callback payloads a recommendation button may carry.

Anchored and explicit: callback_data arrives from the network and is the one
piece of user-controlled input that reaches a database write, so it is matched
against a fixed shape rather than parsed.
"""


@dataclass(frozen=True)
class Handled:
    """What one update resulted in, for logging and for tests."""

    kind: str
    profile: str | None = None
    detail: str | None = None


def _load_offset() -> int:
    """Return the next update id to ask for."""
    if not STATE_PATH.exists():
        return 0
    try:
        return int(json.loads(STATE_PATH.read_text(encoding="utf-8")).get("offset", 0))
    except (OSError, ValueError, json.JSONDecodeError):
        logger.warning("Unreadable daemon state at %s; starting from 0", STATE_PATH)
        return 0


def _save_offset(offset: int) -> None:
    """Persist the next update id, so a restart does not replay the backlog."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"offset": offset}), encoding="utf-8")


def _profile_for(telegram_id: int):  # type: ignore[no-untyped-def]
    """Return the enabled profile owning *telegram_id*, or None."""
    from profiles import ProfileConfigError, load_profiles

    try:
        profiles = load_profiles()
    except ProfileConfigError as exc:
        logger.error("Cannot route updates: %s", exc)
        return None
    for profile in profiles.values():
        if profile.telegram_id == telegram_id and profile.enabled:
            return profile
    return None


def _record_decision(profile, recommendation_id: int, decision: str) -> str:  # type: ignore[no-untyped-def]
    """Record a button press against a recommendation.

    Returns:
        A short line to show in the button's toast.
    """
    from store import open_existing_db

    conn = open_existing_db(profile.db)
    row = conn.execute(
        "SELECT id, action, expires_on FROM recommendation WHERE id = ?",
        (recommendation_id,),
    ).fetchone()
    if row is None:
        return "That recommendation no longer exists."
    if row["expires_on"] < date.today().isoformat():
        return f"Expired on {row['expires_on']} — it has been superseded."

    with conn:
        conn.execute(
            """
            INSERT INTO user_decision (recommendation_id, decision, note, decided_at)
            VALUES (?, ?, ?, ?)
            """,
            (row["id"], decision, "via Telegram", datetime.now(UTC).isoformat()),
        )
    if decision == "approve":
        return "Recorded. Nothing has been traded — execute it yourself."
    return f"Recorded: {decision}."


def _chat_reply(profile, text: str) -> str:  # type: ignore[no-untyped-def]
    """Answer one of the bot's slash commands."""
    from report import weekly_report
    from store import open_existing_db

    command = text.strip().split()[0].lstrip("/").split("@")[0].lower()
    conn = open_existing_db(profile.db)

    if command in {"review", "start"}:
        return weekly_report(conn, profile=profile).body
    if command == "holdings":
        from portfolio import cash_eur, holdings, total_value

        rows = holdings(conn, account_id=1)
        cash = cash_eur(conn, account_id=1)
        lines = [f"<b>€{total_value(rows, cash=cash):,.2f}</b>", ""]
        for row in sorted(rows, key=lambda r: -(r.value_eur or 0)):
            value = f"€{row.value_eur:,.2f}" if row.value_eur is not None else "—"
            change = (
                f" ({row.unrealised_return_pct:+.1f}%)"
                if row.unrealised_return_pct is not None
                else ""
            )
            lines.append(f"{row.position.security.ticker}  {value}{change}")
        return "\n".join(lines)
    if command == "pending":
        parts = weekly_report(conn, profile=profile)
        if not parts.actionable:
            return "Nothing is waiting on you."
        return "\n".join(
            f"{item['action']} {item['ticker'] or ''} — {item['rationale']}"
            for item in parts.actionable
        )
    return "I know /review, /holdings and /pending."


def handle_update(update: dict) -> Handled:
    """Route one update to the profile that owns it.

    Args:
        update: A Telegram update object.

    Returns:
        What was done with it.
    """
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        sender = int(callback.get("from", {}).get("id", 0))
        profile = _profile_for(sender)
        if profile is None:
            # Silence rather than a refusal: answering would confirm the bot is
            # live and tell a stranger their id is simply not on the list.
            logger.warning("Ignoring callback from unknown Telegram id %s", sender)
            return Handled(kind="ignored", detail="unknown sender")

        match = _CALLBACK.match(str(callback.get("data", "")))
        if match is None:
            answer_callback(callback_id=callback["id"], text="Unrecognised button.")
            return Handled(kind="bad_callback", profile=profile.name)

        message = _record_decision(profile, int(match.group(1)), match.group(2))
        answer_callback(callback_id=callback["id"], text=message)
        return Handled(kind="decision", profile=profile.name, detail=match.group(2))

    message = update.get("message")
    if isinstance(message, dict):
        sender = int(message.get("from", {}).get("id", 0))
        profile = _profile_for(sender)
        if profile is None:
            logger.warning("Ignoring message from unknown Telegram id %s", sender)
            return Handled(kind="ignored", detail="unknown sender")
        text = str(message.get("text", "")).strip()
        if not text:
            return Handled(kind="empty", profile=profile.name)
        send_message(chat_id=profile.telegram_id, text=_chat_reply(profile, text))
        return Handled(kind="reply", profile=profile.name, detail=text.split()[0])

    return Handled(kind="skipped")


def run_daemon(*, stop_after: int | None = None) -> None:
    """Poll Telegram until stopped.

    Args:
        stop_after: Stop after this many polling cycles. For tests; None runs
            until the process is killed.

    Raises:
        TelegramError: If another poller is already running for this bot.
    """
    offset = _load_offset()
    logger.info("Daemon started; polling from offset %d", offset)
    cycles = 0

    while stop_after is None or cycles < stop_after:
        cycles += 1
        try:
            updates = get_updates(offset=offset, timeout=DAEMON_POLL_TIMEOUT_S)
        except TelegramError as exc:
            if "Another poller" in str(exc):
                raise
            logger.warning(
                "Poll failed: %s; retrying in %ds", exc, DAEMON_ERROR_BACKOFF_S
            )
            time.sleep(DAEMON_ERROR_BACKOFF_S)
            continue

        for update in updates:
            update_id = int(update.get("update_id", 0))
            try:
                result = handle_update(update)
                logger.info("Handled update %d: %s", update_id, result.kind)
            except Exception:  # noqa: BLE001 - one bad update must not stop the loop
                logger.exception("Failed to handle update %d", update_id)
            # Advanced even on failure: a message that crashes the handler will
            # crash it again on every restart, and a stuck offset means nothing
            # after it is ever seen.
            offset = update_id + 1
            _save_offset(offset)
