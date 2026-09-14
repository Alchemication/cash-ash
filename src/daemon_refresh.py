"""Run a broker refresh off the polling thread and message its progress.

A refresh opens a headful Chrome, waits on phone approval, and downloads a PDF —
tens of seconds at least, sometimes minutes. The poller cannot wait for it, so
it runs on a single worker thread, like a chat turn. One at a time per profile:
Chrome's session is one browser, and two refreshes would fight over it.

Nothing schedules a refresh. The owner starts one when they know something
happened — a buy, a sell — with ``/refresh`` in Telegram or the Run now button.
Queueing therefore only reports ``queued`` or ``busy`` and leaves the wording to
the caller, so a slash command answers with a message and a button with a toast,
and neither says it twice.

Every Telegram message the refresh sends is here, not in ``broker_refresh``: the
proposal and its Run now button, the QR link (only ever in reply to the owner
starting a refresh), the "signed in" confirmation, and the result.
``broker_refresh`` returns a plain result and stays testable without a bot.

Public API:
    offer_broker_refresh -- send the Run now proposal
    submit_broker_refresh -- queue a refresh on the profile's worker
    STARTING_MESSAGE, ALREADY_RUNNING -- what a caller tells the owner
    shutdown             -- stop the worker, for tests and a clean exit
"""

from __future__ import annotations

import asyncio
import html
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

_workers: dict[str, ThreadPoolExecutor] = {}
_running: set[str] = set()
_lock = threading.Lock()

_PROPOSAL = (
    "<b>Broker refresh</b>\n"
    "Download the latest Revolut statement and reconcile it. You will get a "
    "sign-in link to approve on your phone if the session has expired.\n"
    "Run it now?"
)
ALREADY_RUNNING = "A broker refresh is already running; wait for it to finish."
STARTING_MESSAGE = (
    "Starting the broker refresh — a sign-in link will arrive if sign-in is needed."
)


def offer_broker_refresh(profile) -> None:  # type: ignore[no-untyped-def]
    """Send the Run now proposal for a broker refresh."""
    from notify import send_with_buttons

    send_with_buttons(
        chat_id=profile.telegram_id,
        text=_PROPOSAL,
        buttons=[[("Run now", "refresh:run")]],
    )


def submit_broker_refresh(profile, *, start=None, end=None) -> str:  # type: ignore[no-untyped-def]
    """Queue one refresh on the profile's worker, refusing a concurrent one.

    Sends nothing itself: the caller knows whether it is answering a slash
    command or a button, and two acknowledgements for one tap read as a bug.

    Args:
        profile: Whose refresh it is.
        start: First day of the period; defaults to the current calendar month.
        end: Last day of the period.

    Returns:
        ``queued`` or ``busy``.
    """
    with _lock:
        if profile.name in _running:
            logger.info("Refusing a second refresh for %s", profile.name)
            return "busy"
        worker = _workers.get(profile.name)
        if worker is None:
            worker = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=f"refresh-{profile.name}"
            )
            _workers[profile.name] = worker
        _running.add(profile.name)

    worker.submit(_run_and_release, profile, start, end)
    return "queued"


def _run_and_release(profile, start, end) -> None:  # type: ignore[no-untyped-def]
    """Run one refresh and free the per-profile slot whatever happened."""
    try:
        _run_refresh(profile, start, end)
    except Exception:  # noqa: BLE001 - one bad refresh must not kill the worker
        logger.exception("Broker refresh failed for %s", profile.name)
        _say(
            profile, "❌ The broker refresh crashed. It is in the log; nothing changed."
        )
    finally:
        with _lock:
            _running.discard(profile.name)


def _run_refresh(profile, start, end) -> None:  # type: ignore[no-untyped-def]
    """Drive one refresh, sending the link, the confirmation and the result."""
    from broker_refresh import run_broker_refresh

    async def deliver_link(link: str) -> None:
        await asyncio.to_thread(_send_login_link, profile, link)

    async def on_signed_in() -> None:
        await asyncio.to_thread(
            _say, profile, "🔓 Signed in — downloading the statement now."
        )

    result = asyncio.run(
        run_broker_refresh(
            profile,
            deliver_link=deliver_link,
            on_signed_in=on_signed_in,
            start=start,
            end=end,
        )
    )
    logger.info(
        "Broker refresh for %s finished: %s — %s",
        profile.name,
        result.status,
        result.detail,
    )
    _say(profile, _result_text(result))


def _result_text(result) -> str:  # type: ignore[no-untyped-def]
    """Render a refresh result as one Telegram message."""
    from notify import escape

    if result.status == "reconciled":
        return f"✅ <b>Broker refresh done.</b>\n{escape(result.detail)}"
    if result.status == "sign_in_needed":
        return (
            f"⚠️ <b>Revolut sign-in needs you at the Mac.</b>\n{escape(result.detail)}"
        )
    if result.status == "not_approved":
        return (
            "⌛ <b>The sign-in link was not approved in time.</b>\n"
            "Send /refresh to try again and approve on your phone within the window."
        )
    return f"❌ <b>Broker refresh failed.</b>\n{escape(result.detail)}"


def _send_login_link(profile, link: str) -> None:  # type: ignore[no-untyped-def]
    """Send the QR login link as a tappable message, escaping the URL."""
    from notify import send_message

    safe = html.escape(link, quote=True)
    send_message(
        chat_id=profile.telegram_id,
        text=(
            "🔑 <b>Revolut sign-in</b> — approve only if you started this refresh "
            f'just now; the link expires shortly.\n<a href="{safe}">Open Revolut '
            "approval</a>"
        ),
    )


def _say(profile, text: str) -> None:  # type: ignore[no-untyped-def]
    """Send one status line, swallowing a delivery failure into the log."""
    from notify import TelegramError, send_message

    try:
        send_message(chat_id=profile.telegram_id, text=text)
    except TelegramError:
        logger.exception("Could not send a refresh update to %s", profile.name)


def shutdown(wait: bool = True) -> None:
    """Stop every refresh worker. For a clean exit and for tests."""
    with _lock:
        workers = list(_workers.values())
        _workers.clear()
        _running.clear()
    for worker in workers:
        worker.shutdown(wait=wait)
