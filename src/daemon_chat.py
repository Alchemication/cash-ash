"""Running a chat turn for Telegram, off the polling thread.

A chat turn takes seconds: a model call, a tool, often another model call. The
poller cannot wait for it. Before this, every update was handled inline on the
polling loop, which was fine when handling one meant reading the database — one
slow turn would now stall every button press and every other message behind it.

So each profile gets one worker thread. One, not a pool: turns from the same
person have to run in order, or a follow-up question is answered before the
question it follows. A small backlog is allowed and then shed, because someone
typing faster than the model answers is better told so than queued indefinitely.

The person sees a placeholder immediately, which is then rewritten with the
answer. Deliberately static text rather than an animation — an animating thread
editing the same message can land *after* the final edit and overwrite the
answer with "Working…", which is how zdrowskit lost replies.

Public API:
    submit_chat_turn  -- queue one question for a profile
    run_chat_turn     -- answer one question, placeholder to reply
    conversation_for  -- that profile's running conversation
    shutdown          -- stop the workers, for tests and a clean exit

Example:
    from daemon_chat import submit_chat_turn

    submit_chat_turn(profile, "how much cash do I have?")
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from chat import ConversationBuffer

logger = logging.getLogger(__name__)

MAX_PENDING_TURNS = 2
"""Questions allowed to wait on one profile's worker before shedding.

Two is a question being answered plus one typed while waiting. Past that the
person is queueing faster than the model answers, and every extra turn makes
their own next reply later. Telling them to resend is more honest than a queue
that silently grows.
"""

_PLACEHOLDER = "Working…"
"""What the message says until the answer replaces it."""

_BUSY = "Still working through your last question — send this one again once I reply."

_FAILED = (
    "Something went wrong answering that. It is in the log; try again, or ask "
    "it more narrowly."
)

_workers: dict[str, ThreadPoolExecutor] = {}
_conversations: dict[str, ConversationBuffer] = {}
_pending: dict[str, int] = {}
_lock = threading.Lock()


def conversation_for(profile_name: str) -> ConversationBuffer:
    """Return the running conversation for a profile, creating it if needed."""
    with _lock:
        return _conversations.setdefault(profile_name, ConversationBuffer())


def reset_conversation(profile_name: str) -> None:
    """Forget a profile's conversation, so the next question starts clean."""
    conversation_for(profile_name).clear()


def submit_chat_turn(profile, text: str) -> str:  # type: ignore[no-untyped-def]
    """Queue one question on the profile's worker thread.

    Args:
        profile: Whose question it is.
        text: What they asked.

    Returns:
        ``queued`` or ``shed``, for the caller's log.
    """
    from notify import TelegramError, send_message

    with _lock:
        if _pending.get(profile.name, 0) >= MAX_PENDING_TURNS:
            logger.warning(
                "Profile %s has %d chat turns queued; shedding this one",
                profile.name,
                _pending[profile.name],
            )
            try:
                send_message(chat_id=profile.telegram_id, text=_BUSY)
            except TelegramError:
                logger.exception("Could not tell %s the queue is full", profile.name)
            return "shed"
        worker = _workers.get(profile.name)
        if worker is None:
            worker = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=f"chat-{profile.name}"
            )
            _workers[profile.name] = worker
        _pending[profile.name] = _pending.get(profile.name, 0) + 1

    worker.submit(_run_and_release, profile, text)
    return "queued"


def _run_and_release(profile, text: str) -> None:  # type: ignore[no-untyped-def]
    """Run one turn and free its backlog slot whatever happened."""
    try:
        run_chat_turn(profile, text)
    except Exception:  # noqa: BLE001 - one bad turn must not kill the worker
        logger.exception("Chat turn failed for %s", profile.name)
    finally:
        with _lock:
            _pending[profile.name] = max(_pending.get(profile.name, 1) - 1, 0)


def run_chat_turn(profile, text: str) -> None:  # type: ignore[no-untyped-def]
    """Answer one question: placeholder, model, reply.

    The conversation is only appended to once an answer exists. A failed turn
    leaves no trace in the buffer, so the next question is not preceded by a
    question that was never answered.

    Args:
        profile: Whose question it is.
        text: What they asked.
    """
    from chat import answer
    from notify import TelegramError, escape, send_message
    from store import open_existing_db

    conversation = conversation_for(profile.name)
    history = [*conversation.messages(), {"role": "user", "content": text}]

    try:
        placeholder = send_message(chat_id=profile.telegram_id, text=_PLACEHOLDER)
        message_id = placeholder[0] if placeholder else 0
    except TelegramError:
        logger.exception("Could not acknowledge a question from %s", profile.name)
        return

    try:
        with open_existing_db(profile.db) as conn:
            result = answer(conn, db_path=profile.db, history=history)
    except Exception:  # noqa: BLE001 - the person is waiting on a placeholder
        logger.exception("Chat could not answer for %s", profile.name)
        _replace(profile, message_id, _FAILED)
        return

    logger.info(
        "Chat answered for %s in %d iteration(s), tools=%s%s",
        profile.name,
        result.iterations,
        ",".join(result.tools_called) or "none",
        " (degraded)" if result.degraded else "",
    )
    conversation.add("user", text)
    conversation.add("assistant", result.text)
    # Escaped, not trusted: the answer carries figures and tickers read out of
    # the database, and an ampersand in a company name is enough for Telegram to
    # reject the whole message as broken HTML.
    _replace(profile, message_id, escape(result.text))


def _replace(profile, message_id: int, text: str) -> None:  # type: ignore[no-untyped-def]
    """Rewrite the placeholder, or send a new message if that fails."""
    from notify import TelegramError, edit_message, send_message

    try:
        if message_id:
            edit_message(chat_id=profile.telegram_id, message_id=message_id, text=text)
        else:
            send_message(chat_id=profile.telegram_id, text=text)
        return
    except TelegramError:
        logger.warning("Could not edit the placeholder for %s", profile.name)
    try:
        send_message(chat_id=profile.telegram_id, text=text)
    except TelegramError:
        logger.exception("Could not deliver an answer to %s", profile.name)


def shutdown(wait: bool = True) -> None:
    """Stop every worker. For a clean exit and for tests."""
    with _lock:
        workers = list(_workers.values())
        _workers.clear()
        _pending.clear()
    for worker in workers:
        worker.shutdown(wait=wait)
