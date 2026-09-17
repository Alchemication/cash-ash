"""Sending the weekly review to Telegram: live progress, summary, cards.

Three places send a review — the daemon's schedule, ``main.py weekly
--telegram`` and ``main.py report --telegram`` — and each carried its own copy
of the sending loop. That is how the rationale came to be printed twice, in
the summary and again on every card, without anyone deciding it should be.
They share this module now.

Public API:
    LiveProgress -- post the stage flow and edit it as each stage starts
    send_review  -- send the summary, replacing the progress message, then cards

Example:
    progress = LiveProgress(chat_id=profile.telegram_id, run_date=date.today())
    outcome = run_weekly(conn, profile=profile, progress=progress)
    send_review(conn, profile=profile, progress_message_id=progress.message_id)
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date

from notify import TelegramError, delete_message, edit_message, send_message
from report import ReportParts, card_text, progress_text, weekly_report

logger = logging.getLogger(__name__)


class LiveProgress:
    """One Telegram message that follows the weekly run stage by stage.

    Edited rather than re-sent, so a run occupies a single message however long
    it takes. Sent silently: the start of a run is not news, and the finished
    summary is sent as a fresh message precisely so that it is the one that
    notifies.
    """

    def __init__(self, *, chat_id: int, run_date: date) -> None:
        """Prepare a progress message; nothing is sent until the first stage.

        Args:
            chat_id: Numeric Telegram chat to post in.
            run_date: Date the run is for, shown in the title.
        """
        self.chat_id = chat_id
        self.run_date = run_date
        self.message_id: int | None = None
        self._last = ""

    def __call__(
        self, stages: list, running: str | None, note: str | None = None
    ) -> None:
        """Show finished stages and the one starting.

        Args:
            stages: Stages finished so far.
            running: Stage now starting, or None.
            note: What the running stage is on, e.g. ``2/4 TEST``.

        Raises:
            TelegramError: If the send or edit failed. The weekly run catches
                it; progress is a courtesy and never stops the run.
        """
        text = progress_text(stages, running, note, run_date=self.run_date)
        # Telegram refuses an edit that changes nothing.
        if text == self._last:
            return
        if self.message_id is None:
            self.message_id = send_message(
                chat_id=self.chat_id, text=text, silent=True
            )[0]
        else:
            edit_message(chat_id=self.chat_id, message_id=self.message_id, text=text)
        self._last = text


def send_review(
    conn: sqlite3.Connection,
    *,
    profile,  # type: ignore[no-untyped-def]
    progress_message_id: int | None = None,
    today: date | None = None,
) -> ReportParts:
    """Send the summary, then one card per recommendation.

    Read-only: the cards carry no buttons. The owner trades at the broker and
    syncs, and whether they followed a recommendation is read from the ledger,
    so a second workflow of approvals would only be a record of intentions
    nobody keeps up to date.

    The progress message is deleted and the summary sent fresh rather than
    edited into place. An edit never notifies, so a run that ended in an edit
    would reach the phone in silence; and a fourteen-minute run can have chat
    messages below its progress message, which would bury the summary above
    them.

    Args:
        conn: Open database connection.
        profile: Profile whose chat receives the review.
        progress_message_id: A live progress message to remove, if any.
        today: Reference date, for tests.

    Returns:
        What was sent.

    Raises:
        TelegramError: If the summary or a card could not be sent.
    """
    parts = weekly_report(conn, profile=profile, today=today)
    if progress_message_id is not None:
        try:
            delete_message(chat_id=profile.telegram_id, message_id=progress_message_id)
        except TelegramError:
            # A leftover progress message is untidy, not wrong; the summary
            # below it is still the answer.
            logger.warning("Could not remove the progress message", exc_info=True)
    send_message(chat_id=profile.telegram_id, text=parts.body)
    for item in parts.actionable:
        send_message(chat_id=profile.telegram_id, text=card_text(item))
    return parts
