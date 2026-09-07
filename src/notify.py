"""Sending messages to Telegram.

Deliberately small. skarbie builds its own reports, so it emits Telegram's HTML
subset directly rather than converting arbitrary markdown into it — the
conversion is the part that goes wrong, and there is nothing here to convert.

Public API:
    send_message      -- send one message, chunked and retried
    send_with_buttons -- send a message carrying an inline keyboard
    answer_callback   -- acknowledge a button press
    escape            -- escape text for Telegram's HTML mode
    TelegramError     -- the send failed and could not be retried

Example:
    from notify import send_message

    send_message(chat_id=12345, text="<b>Portfolio review</b>\\nNothing to do.")
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from html import escape as _html_escape

from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_MAX_MESSAGE_CHARS,
    TELEGRAM_RETRY_DELAYS,
    TELEGRAM_TIMEOUT_S,
)

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(RuntimeError):
    """Raised when Telegram rejects a request or cannot be reached."""


def escape(text: str) -> str:
    """Escape text for Telegram's HTML parse mode.

    Args:
        text: Raw text, possibly containing angle brackets or ampersands.

    Returns:
        Text safe to place inside an HTML-mode message.
    """
    return _html_escape(str(text), quote=False)


def _require_token() -> str:
    """Return the bot token, or explain how to get one.

    Raises:
        TelegramError: If no token is configured.
    """
    if not TELEGRAM_BOT_TOKEN:
        raise TelegramError(
            "No TELEGRAM_BOT_TOKEN in .env. Create a bot by messaging "
            "@BotFather on Telegram, then put the token it gives you in .env. "
            "One bot serves every profile; people are told apart by the "
            "telegram_id in profiles.toml."
        )
    return TELEGRAM_BOT_TOKEN


def _call(method: str, payload: dict) -> dict:
    """Call one Telegram API method, retrying transient failures.

    Args:
        method: API method name.
        payload: JSON body.

    Returns:
        The ``result`` object from Telegram's response.

    Raises:
        TelegramError: If Telegram refused the request, or every retry failed.
    """
    token = _require_token()
    url = _API.format(token=token, method=method)
    body = json.dumps(payload).encode("utf-8")
    last: Exception | None = None

    for attempt, delay in enumerate((*TELEGRAM_RETRY_DELAYS, None)):
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(
                request, timeout=TELEGRAM_TIMEOUT_S
            ) as response:
                parsed = json.loads(response.read().decode("utf-8"))
            if not parsed.get("ok"):
                # A refusal is about the message, not the connection: a bad
                # chat id or malformed HTML will be refused identically every
                # time, so retrying only delays the error.
                raise TelegramError(_explain(method, str(parsed.get("description"))))
            return parsed.get("result", {})
        except urllib.error.HTTPError as exc:
            detail = _error_detail(exc)
            if exc.code < 500:
                raise TelegramError(_explain(method, detail)) from exc
            last = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc

        if delay is None:
            break
        logger.warning(
            "Telegram %s failed (attempt %d): %s; retrying in %ds",
            method,
            attempt + 1,
            last,
            delay,
        )
        time.sleep(delay)

    raise TelegramError(f"Telegram {method} failed after every attempt: {last}")


def _explain(method: str, detail: str) -> str:
    """Turn Telegram's terse refusal into something actionable.

    Telegram's own wording describes its internal state rather than what the
    caller should do about it, and one refusal in particular is a rule nobody
    knows until they hit it: a bot may not open a conversation. Until the
    person sends it a message, their chat does not exist as far as the API is
    concerned, and the id being correct makes no difference.
    """
    lowered = detail.lower()
    if "chat not found" in lowered:
        return (
            "Telegram will not let a bot message someone who has never "
            "messaged it. Open the bot in Telegram and send it anything — "
            "/start will do — then try again. The chat id is not the problem."
        )
    if "bot was blocked" in lowered:
        return (
            "That person has blocked the bot, so nothing can be delivered to "
            "them until they unblock it."
        )
    if "can't parse entities" in lowered or "unsupported start tag" in lowered:
        return (
            f"Telegram rejected the message formatting: {detail}. Something in "
            f"the text was not escaped for HTML mode."
        )
    return f"Telegram refused {method}: {detail}"


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """Return Telegram's own description of a failure where it gave one."""
    try:
        body = json.loads(exc.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - the body is best-effort
        return f"HTTP {exc.code}"
    return str(body.get("description") or f"HTTP {exc.code}")


def chunk(text: str, limit: int = TELEGRAM_MAX_MESSAGE_CHARS) -> list[str]:
    """Split text into messages Telegram will accept.

    Splits on blank lines, then on single lines, so a report breaks between
    sections rather than mid-sentence. A single line longer than the limit is
    hard-split, which is ugly but preferable to losing it.

    Args:
        text: Message body.
        limit: Maximum characters per message.

    Returns:
        One or more chunks, each within the limit.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(block) > limit:
            cut = block.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            chunks.append(block[:cut])
            block = block[cut:].lstrip("\n")
        current = block
    if current:
        chunks.append(current)
    return chunks


def send_message(*, chat_id: int, text: str, silent: bool = False) -> list[int]:
    """Send a message, splitting it if Telegram would refuse the length.

    Args:
        chat_id: Numeric Telegram user or chat id.
        text: Message body in Telegram's HTML subset.
        silent: Deliver without a notification sound.

    Returns:
        The message ids sent, in order.

    Raises:
        TelegramError: If the send failed.
    """
    ids: list[int] = []
    for part in chunk(text):
        result = _call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": part,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": silent,
            },
        )
        ids.append(int(result.get("message_id", 0)))
    return ids


def send_with_buttons(
    *, chat_id: int, text: str, buttons: list[list[tuple[str, str]]]
) -> int:
    """Send one message carrying an inline keyboard.

    Not chunked: a message with buttons has to stay one message, or the buttons
    end up detached from what they act on.

    Args:
        chat_id: Numeric Telegram user or chat id.
        text: Message body in Telegram's HTML subset.
        buttons: Rows of ``(label, callback_data)`` pairs.

    Returns:
        The message id.

    Raises:
        TelegramError: If the send failed.
        ValueError: If the text is too long to send with buttons attached.
    """
    if len(text) > TELEGRAM_MAX_MESSAGE_CHARS:
        raise ValueError(
            f"A message with buttons cannot be split, and this one is "
            f"{len(text)} characters against a {TELEGRAM_MAX_MESSAGE_CHARS} "
            f"limit. Shorten it rather than dropping the buttons."
        )
    result = _call(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": label, "callback_data": data} for label, data in row]
                    for row in buttons
                ]
            },
        },
    )
    return int(result.get("message_id", 0))


def answer_callback(*, callback_id: str, text: str | None = None) -> None:
    """Acknowledge a button press so Telegram stops showing a spinner."""
    payload: dict = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    _call("answerCallbackQuery", payload)


def set_commands(commands: list[tuple[str, str]]) -> None:
    """Register the bot's command list for Telegram's menu.

    Args:
        commands: ``(command, description)`` pairs, without leading slashes.
    """
    _call(
        "setMyCommands",
        {
            "commands": [
                {"command": name, "description": description}
                for name, description in commands
            ]
        },
    )


def get_updates(*, offset: int, timeout: int = 30) -> list[dict]:
    """Long-poll Telegram for new updates.

    Args:
        offset: Return only updates with an id at or above this. Acknowledging
            by offset is how Telegram is told an update was handled; without it
            every restart replays the backlog.
        timeout: Seconds to hold the connection open waiting for something.

    Returns:
        Update objects, oldest first.

    Raises:
        TelegramError: If Telegram refused, including the 409 that means
            another poller is already running for this bot.
    """
    token = _require_token()
    url = (
        _API.format(token=token, method="getUpdates")
        + f"?offset={offset}&timeout={timeout}"
    )
    try:
        with urllib.request.urlopen(
            url, timeout=timeout + TELEGRAM_TIMEOUT_S
        ) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            raise TelegramError(
                "Another poller is already running for this bot. Two pollers "
                "steal each other's updates, so button presses would be "
                "handled at random or not at all. Stop the other one first."
            ) from exc
        raise TelegramError(f"getUpdates refused: {_error_detail(exc)}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TelegramError(f"getUpdates failed: {exc}") from exc

    if not parsed.get("ok"):
        raise TelegramError(f"getUpdates not ok: {parsed.get('description')}")
    return list(parsed.get("result", []))
