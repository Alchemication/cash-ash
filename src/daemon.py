"""The background process: listens for replies, and runs the week when due.

One process with two threads, following zdrowskit. A scheduler wakes
periodically and asks whether this week's run has happened; the Telegram poller
waits on updates. One launchd job, one log, one thing that can be broken.

The scheduler asks a question about *state* — has a run been recorded for this
ISO week? — rather than watching for a moment to pass. A machine asleep at the
scheduled hour therefore runs on waking, instead of skipping the week, which is
the failure that has already cost this project one overnight run.

Every update is routed by the sender's numeric Telegram id to a profile in the
roster. An update from an id that is not in the roster is ignored in silence —
not answered, not acknowledged — because the bot's username is discoverable and
a stranger who finds it must not be able to read anyone's portfolio or learn
that the account exists.

Public API:
    run_daemon      -- run the scheduler and, if configured, the listener
    handle_update   -- route one update; the unit the tests exercise
    weekly_is_due   -- whether this week's run still needs to happen

Example:
    from daemon import run_daemon

    run_daemon()
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime

from config import (
    APP_HOME,
    DAEMON_ERROR_BACKOFF_S,
    DAEMON_POLL_TIMEOUT_S,
    SCHEDULED_CHECK_INTERVAL_S,
    WEEKLY_RUN_HOUR,
    WEEKLY_RUN_WEEKDAY,
)
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
    try:
        return int(_read_state().get("offset", 0))
    except (TypeError, ValueError):
        return 0


def _save_offset(offset: int) -> None:
    """Persist the next update id, so a restart does not replay the backlog.

    Merged into the shared state rather than overwriting it: the file also
    carries when each profile last had its weekly run, and replacing it here
    would silently reschedule everyone's week on the next button press.
    """
    _write_state(offset=offset)


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


def _read_state() -> dict:
    """Return the daemon's persisted state."""
    if not STATE_PATH.exists():
        return {}
    try:
        loaded = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        logger.warning("Unreadable daemon state at %s; starting fresh", STATE_PATH)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_state(**changes) -> None:  # type: ignore[no-untyped-def]
    """Merge *changes* into the persisted state."""
    state = _read_state()
    state.update(changes)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


def weekly_is_due(profile_name: str, *, now: datetime | None = None) -> bool:
    """Return True when this week's run has not happened yet and its time has passed.

    Keyed on the ISO week rather than on a timestamp comparison. Asking "has a
    run been recorded for this week?" makes a missed slot self-correcting: a
    machine asleep on Sunday evening runs on Monday instead of skipping, and a
    daemon restarted twice in an hour does not run twice.

    Args:
        profile_name: Whose run is being considered.
        now: Reference time, for tests.

    Returns:
        Whether to run.
    """
    moment = now or datetime.now()
    year, week, _ = moment.isocalendar()
    if _read_state().get("last_weekly", {}).get(profile_name) == f"{year}-W{week:02d}":
        return False

    # launchd weekdays put Sunday at 0; Python puts Monday at 0 and Sunday at 6.
    weekday = (moment.weekday() + 1) % 7
    if weekday < WEEKLY_RUN_WEEKDAY:
        return False
    return not (weekday == WEEKLY_RUN_WEEKDAY and moment.hour < WEEKLY_RUN_HOUR)


def _record_weekly(profile_name: str, *, now: datetime | None = None) -> None:
    """Mark this week's run as done for a profile."""
    moment = now or datetime.now()
    year, week, _ = moment.isocalendar()
    state = _read_state()
    last = dict(state.get("last_weekly", {}))
    last[profile_name] = f"{year}-W{week:02d}"
    _write_state(last_weekly=last)


def _run_weekly_for_profiles(send: bool = True) -> None:
    """Run the weekly cycle for every enabled profile whose turn it is."""
    from profiles import ProfileConfigError, load_profiles
    from store import open_existing_db
    from weekly import run_weekly

    try:
        profiles = load_profiles()
    except ProfileConfigError as exc:
        logger.error("Cannot run the week: %s", exc)
        return

    for profile in profiles.values():
        if not profile.enabled or not weekly_is_due(profile.name):
            continue
        logger.info("Weekly run starting for %s", profile.name)
        try:
            outcome = run_weekly(open_existing_db(profile.db), profile=profile)
        except Exception:  # noqa: BLE001 - one profile must not stop the others
            logger.exception("Weekly run failed for %s", profile.name)
            continue

        # Recorded even when stages failed. The run happened; retrying it every
        # thirty minutes for the rest of the week would be worse than waiting,
        # and the report says what went wrong.
        _record_weekly(profile.name)
        if send and outcome.report:
            _send_weekly(profile, outcome)
        logger.info(
            "Weekly run finished for %s: %s",
            profile.name,
            "; ".join(f"{s.name} {'ok' if s.ok else 'failed'}" for s in outcome.stages),
        )


def _send_weekly(profile, outcome) -> None:  # type: ignore[no-untyped-def]
    """Send a finished run's report, with buttons on anything actionable."""
    from notify import send_message, send_with_buttons
    from report import weekly_report
    from store import open_existing_db

    try:
        parts = weekly_report(open_existing_db(profile.db), profile=profile)
        send_message(chat_id=profile.telegram_id, text=parts.body)
        for item in parts.actionable:
            ticker = f"{item['ticker']} " if item["ticker"] else ""
            amount = f" — €{item['amount_eur']:,.2f}" if item["amount_eur"] else ""
            send_with_buttons(
                chat_id=profile.telegram_id,
                text=f"<b>{item['action']}</b> {ticker}{amount}\n{item['rationale']}",
                buttons=[
                    [
                        ("Approve", f"rec:{item['id']}:approve"),
                        ("Reject", f"rec:{item['id']}:reject"),
                        ("Later", f"rec:{item['id']}:later"),
                    ]
                ],
            )
    except Exception:  # noqa: BLE001 - the run itself already succeeded
        logger.exception("Could not send the report for %s", profile.name)


def _scheduler_loop(stop: threading.Event) -> None:
    """Check periodically whether a weekly run is due."""
    while not stop.wait(SCHEDULED_CHECK_INTERVAL_S):
        try:
            _run_weekly_for_profiles()
        except Exception:  # noqa: BLE001 - the scheduler must outlive a bad week
            logger.exception("Scheduler cycle failed")


def run_daemon(
    *, stop_after: int | None = None, stop: threading.Event | None = None
) -> None:
    """Run the scheduler, and the Telegram listener when one is configured.

    Args:
        stop_after: Stop the listener after this many polling cycles. For
            tests; None runs until the process is killed.
        stop: Event that ends the scheduler thread. For tests.

    Raises:
        TelegramError: If another poller is already running for this bot.
    """
    from config import TELEGRAM_BOT_TOKEN

    stop_event = stop or threading.Event()
    scheduler = threading.Thread(
        target=_scheduler_loop, args=(stop_event,), daemon=True, name="scheduler"
    )
    scheduler.start()
    logger.info(
        "Scheduler running; the weekly review fires on day %d after %02d:00",
        WEEKLY_RUN_WEEKDAY,
        WEEKLY_RUN_HOUR,
    )

    if not TELEGRAM_BOT_TOKEN:
        # Not a transient fault, so not retried — the first version looped on
        # this every fifteen seconds forever. The scheduler still runs: a
        # weekly review is worth having even when there is nowhere to send it,
        # since it is all recorded and readable from the CLI.
        logger.warning(
            "No TELEGRAM_BOT_TOKEN set, so nothing is listening for replies. "
            "The weekly run still happens. Add a token to .env and run "
            "'main.py daemon restart' to enable buttons."
        )
        if stop_after is not None:
            # Under test, or asked for a bounded run: do not block on the
            # scheduler's interval, which is measured in tens of minutes.
            return
        try:
            while not stop_event.wait(SCHEDULED_CHECK_INTERVAL_S):
                pass
        except KeyboardInterrupt:
            logger.info("Shutting down")
        return

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
