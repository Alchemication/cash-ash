"""The background process: listens for replies, and runs the week when due.

One process, a scheduler thread and a Telegram poller, following zdrowskit. The
scheduler wakes periodically and asks whether this week's run has happened; the
poller waits on updates. One launchd job, one log, one thing that can be broken.

A question in plain words is answered by the chat agent, which takes seconds and
therefore runs on a per-profile worker thread rather than on the poller — see
``daemon_chat``. A slash command is answered inline, because it only reads the
database.

The scheduler asks a question about *state* — has a run been recorded for this
ISO week? — rather than watching for a moment to pass. A machine asleep at the
scheduled hour therefore runs on waking, instead of skipping the week, which is
the failure that has already cost this project one overnight run.

Every update is routed by the sender's numeric Telegram id to a profile in the
roster, and only from a private chat whose id is that same sender. An update
failing either test is ignored in silence — not answered, not acknowledged —
because the bot's username is discoverable and a stranger who finds it must not
be able to read anyone's portfolio or learn that the account exists. The private
chat test is what stops a holding being read out into a group.

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
from review_text import recommendation_buttons
from notify import escape
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as time_of_day

from config import (
    APP_HOME,
    DAEMON_ERROR_BACKOFF_S,
    DAEMON_POLL_TIMEOUT_S,
    SCHEDULED_CHECK_INTERVAL_S,
    WEEKLY_RUN_HOUR,
    WEEKLY_RUN_WEEKDAY,
)
from daemon_chat import submit_chat_turn
from notify import TelegramError, answer_callback, get_updates, send_message

logger = logging.getLogger(__name__)

STATE_PATH = APP_HOME / "daemon_state.json"

_CALLBACK = re.compile(r"^rec:(\d+):(approve|reject|later|evidence|thesis)$")
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

    if telegram_id <= 0:
        # Telegram ids are positive. A zero arrives from an update with no
        # sender at all — a channel post, an anonymous admin — and must never
        # be able to match a roster entry by default.
        return None
    try:
        profiles = load_profiles()
    except ProfileConfigError as exc:
        logger.error("Cannot route updates: %s", exc)
        return None
    for profile in profiles.values():
        if profile.telegram_id == telegram_id and profile.enabled:
            return profile
    return None


def _sender_id(sender: object) -> int:
    """Return the numeric id of an update's sender, or 0 when there is none."""
    if not isinstance(sender, dict):
        return 0
    raw = sender.get("id")
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else 0


def _authorised_profile(sender: object, message: object):  # type: ignore[no-untyped-def]
    """Return the profile allowed to act on an update, or None.

    Three conditions, all required. The sender's id must be on the roster and
    enabled. The chat must be private, so the bot never answers into a group
    where people who are not on the roster can read it. And the chat id must
    equal the sender id, which is what makes the first two checks about the
    same person: in a private one-to-one chat the two are always the same
    number, so any update where they differ is not the conversation it claims
    to be.

    Args:
        sender: The update's ``from`` object.
        message: The message the update concerns. For a callback this is the
            message the button is attached to, not the button press.

    Returns:
        The owning profile, or None when any condition fails.
    """
    sender_id = _sender_id(sender)
    if sender_id == 0 or not isinstance(message, dict):
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict) or chat.get("type") != "private":
        return None
    if chat.get("id") != sender_id:
        return None
    return _profile_for(sender_id)


def _record_decision(profile, recommendation_id: int, decision: str) -> str:  # type: ignore[no-untyped-def]
    """Record a button press against a recommendation.

    Returns:
        A short line to show in the button's toast.
    """
    from store import open_existing_db
    from workflow import record_response

    try:
        with open_existing_db(profile.db) as conn:
            return record_response(
                conn, recommendation_id, decision, note="via Telegram"
            )
    except ValueError as exc:
        return str(exc)


def _chat_reply(profile, text: str) -> str:  # type: ignore[no-untyped-def]
    """Answer one of the bot's slash commands."""
    from report import weekly_report
    from store import open_existing_db

    command = text.strip().split()[0].lstrip("/").split("@")[0].lower()
    conn = open_existing_db(profile.db)

    if command in {"evidence", "thesis", "accept", "reject"}:
        from review_text import evidence_text, thesis_text
        from store_workflow import review_thesis
        from store import load_securities

        words = text.split()
        if len(words) < 2:
            return f"Use /{command} TICKER" + (
                " VERSION" if command in {"accept", "reject"} else ""
            )
        if command in {"accept", "reject"}:
            try:
                security = load_securities(conn).get(words[1].upper())
                if security is None or len(words) != 3:
                    raise ValueError(
                        "Use /accept TICKER VERSION or /reject TICKER VERSION."
                    )
                review_thesis(conn, security.id, int(words[2]), command == "accept")
                return "Thesis decision recorded. Run main.py recommend to reconsider actions."
            except ValueError as exc:
                return escape(str(exc))
        return (
            evidence_text(conn, words[1])
            if command == "evidence"
            else thesis_text(conn, words[1])
        )
    if command in {"review", "start"}:
        return weekly_report(conn, profile=profile).body
    if command == "holdings":
        from portfolio import cash_eur, holdings, total_value

        rows = holdings(conn, account_id=1)
        cash = cash_eur(conn, account_id=1)
        from quality import valuation_gaps

        gaps = valuation_gaps(conn, rows, date.today())
        lines = [f"<b>€{total_value(rows, cash=cash):,.2f}</b>", ""]
        if gaps:
            lines.append(escape("; ".join(gaps)))
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
            f"{item['action']} {item['ticker'] or ''} — {escape(item['rationale'])}"
            for item in parts.actionable
        )
    if command == "reset":
        from daemon_chat import reset_conversation

        reset_conversation(profile.name)
        return "Forgotten. The next question starts a new conversation."
    return (
        "Commands: /review, /holdings, /pending, /evidence TICKER, "
        "/thesis TICKER, /reset. Anything else, just ask in plain words — "
        '"what did I pay for BRK.B", "how much cash", "when did I last '
        'add money".'
    )


def handle_update(update: dict) -> Handled:
    """Route one update to the profile that owns it.

    Args:
        update: A Telegram update object.

    Returns:
        What was done with it.
    """
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        profile = _authorised_profile(callback.get("from"), callback.get("message"))
        if profile is None:
            # Silence rather than a refusal: answering would confirm the bot is
            # live and tell a stranger their id is simply not on the list.
            logger.warning(
                "Ignoring callback from unauthorised Telegram id %s",
                _sender_id(callback.get("from")),
            )
            return Handled(kind="ignored", detail="unauthorised sender")

        match = _CALLBACK.match(str(callback.get("data", "")))
        if match is None:
            answer_callback(callback_id=callback["id"], text="Unrecognised button.")
            return Handled(kind="bad_callback", profile=profile.name)

        if match.group(2) in {"evidence", "thesis"}:
            from store import open_existing_db
            from review_text import evidence_text, thesis_text

            with open_existing_db(profile.db) as conn:
                row = conn.execute(
                    "SELECT s.ticker FROM recommendation r JOIN securities s ON s.id=r.security_id WHERE r.id=?",
                    (int(match.group(1)),),
                ).fetchone()
                body = (
                    (evidence_text if match.group(2) == "evidence" else thesis_text)(
                        conn, row["ticker"]
                    )
                    if row
                    else "No holding linked to this recommendation."
                )
            send_message(chat_id=profile.telegram_id, text=body)
            answer_callback(callback_id=callback["id"], text="Details sent.")
            return Handled(kind="reply", profile=profile.name)
        message = _record_decision(profile, int(match.group(1)), match.group(2))
        answer_callback(callback_id=callback["id"], text=message)
        return Handled(kind="decision", profile=profile.name, detail=match.group(2))

    message = update.get("message")
    if isinstance(message, dict):
        profile = _authorised_profile(message.get("from"), message)
        if profile is None:
            logger.warning(
                "Ignoring message from unauthorised Telegram id %s",
                _sender_id(message.get("from")),
            )
            return Handled(kind="ignored", detail="unauthorised sender")
        text = str(message.get("text", "")).strip()
        if not text:
            return Handled(kind="empty", profile=profile.name)
        if not text.startswith("/"):
            # A question, not a command. Queued on the profile's own thread: a
            # turn takes seconds, and the poller cannot wait for it without
            # stalling every other update behind it.
            outcome = submit_chat_turn(profile, text)
            return Handled(kind=outcome, profile=profile.name, detail="question")
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


def _last_slot(now: datetime) -> datetime | None:
    """Return the most recent scheduled moment that has already passed.

    Computed by walking back to the configured weekday rather than comparing
    weekday numbers, because the two calendars disagree in a way that bites.
    ISO weeks start on Monday, so a Sunday run falls at the *end* of its week
    and the very next morning belongs to a new one — keying the schedule on an
    ISO week therefore fired a second run within a day of the first.

    Args:
        now: Reference time.

    Returns:
        The datetime of the last scheduled slot, or None if none has passed.
    """
    for delta in range(8):
        day = (now - timedelta(days=delta)).date()
        if (day.weekday() + 1) % 7 != WEEKLY_RUN_WEEKDAY:
            continue
        slot = datetime.combine(day, time_of_day(hour=WEEKLY_RUN_HOUR))
        if slot <= now:
            return slot
    return None


def weekly_is_due(profile_name: str, *, now: datetime | None = None) -> bool:
    """Return True when the current scheduled slot has not been run yet.

    Keyed on the slot itself, so the question is "has this occurrence
    happened?" rather than "is it that time?". A machine asleep at the
    scheduled hour runs on waking; a daemon restarted twice in an hour does
    not run twice.

    A profile seen for the first time has its current slot recorded without
    running, so installing the daemon on a Saturday does not immediately fire
    the run that was due last Sunday. The first run is the next scheduled one.

    Args:
        profile_name: Whose run is being considered.
        now: Reference time, for tests.

    Returns:
        Whether to run.
    """
    slot = _last_slot(now or datetime.now())
    if slot is None:
        return False

    recorded = _read_state().get("last_weekly", {})
    if profile_name not in recorded:
        _record_slot(profile_name, slot)
        logger.info(
            "First run for %s scheduled from the next slot, not the missed one",
            profile_name,
        )
        return False
    return recorded[profile_name] != slot.isoformat(timespec="minutes")


def _record_slot(profile_name: str, slot: datetime) -> None:
    """Mark one scheduled slot as handled for a profile."""
    last = dict(_read_state().get("last_weekly", {}))
    last[profile_name] = slot.isoformat(timespec="minutes")
    _write_state(last_weekly=last)


def _record_weekly(profile_name: str, *, now: datetime | None = None) -> None:
    """Mark the current slot as run for a profile."""
    slot = _last_slot(now or datetime.now())
    if slot is not None:
        _record_slot(profile_name, slot)


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
                text=f"<b>{item['action']}</b> {ticker}{amount}\n{escape(item['rationale'])}",
                buttons=recommendation_buttons(item),
            )
    except Exception:  # noqa: BLE001 - the run itself already succeeded
        logger.exception("Could not send the report for %s", profile.name)


def _scheduler_loop(stop: threading.Event) -> None:
    """Check periodically whether a weekly run is due."""
    from reminders import send_due_snoozes

    while not stop.wait(SCHEDULED_CHECK_INTERVAL_S):
        for task in (send_due_snoozes, _run_weekly_for_profiles):
            try:
                task()
            except Exception:  # noqa: BLE001 - one delivery must not stop the weekly run
                logger.exception("Scheduler task failed: %s", task.__name__)


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
