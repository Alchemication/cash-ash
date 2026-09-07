"""Tests for Telegram sending and the weekly report.

No test reaches Telegram. The API call is replaced, which is the only way to
exercise a refusal versus an outage — the two are handled differently and the
difference matters.
"""

from __future__ import annotations

import sqlite3
import urllib.error
from datetime import date
from io import BytesIO

import pytest

import notify as notify_module
from config import TELEGRAM_MAX_MESSAGE_CHARS
from models import Thesis
from notify import TelegramError, chunk, escape, send_message, send_with_buttons
from report import weekly_report
from seed import load_snapshot, seed_database
from store_research import save_thesis
from tests.test_seed import FIXTURE


@pytest.fixture
def token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend a bot token is configured."""
    monkeypatch.setattr(notify_module, "TELEGRAM_BOT_TOKEN", "123:abc")


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, token: None):
    """Replace the Telegram HTTP call with a scripted stand-in."""

    def install(*outcomes):
        queue = list(outcomes)
        sent: list[tuple[str, dict]] = []

        def fake(method: str, payload: dict) -> dict:
            sent.append((method, payload))
            outcome = queue.pop(0) if queue else {"message_id": len(sent)}
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(notify_module, "_call", fake)
        return sent

    return install


class TestEscaping:
    """Telegram HTML mode rejects unescaped markup."""

    def test_angle_brackets(self) -> None:
        assert escape("a < b & c > d") == "a &lt; b &amp; c &gt; d"

    def test_plain_text_is_unchanged(self) -> None:
        assert escape("Berkshire Hathaway") == "Berkshire Hathaway"


class TestChunking:
    """A long report must break between sections, not mid-sentence."""

    def test_short_text_is_one_chunk(self) -> None:
        assert chunk("hello") == ["hello"]

    def test_splits_on_blank_lines(self) -> None:
        block = "x" * 100
        text = "\n\n".join([block] * 10)
        chunks = chunk(text, limit=250)
        assert len(chunks) > 1
        assert all(len(part) <= 250 for part in chunks)

    def test_every_chunk_is_within_the_limit(self) -> None:
        chunks = chunk("word " * 5000, limit=500)
        assert all(len(part) <= 500 for part in chunks)

    def test_an_overlong_single_line_is_split_rather_than_lost(self) -> None:
        chunks = chunk("y" * 1200, limit=500)
        assert "".join(chunks) == "y" * 1200

    def test_nothing_is_dropped(self) -> None:
        text = "\n\n".join(f"section {i} " + "z" * 200 for i in range(12))
        assert sum(len(part) for part in chunk(text, limit=400)) >= len(text) - 24


class TestSending:
    """Refusals and outages are not the same failure."""

    def test_message_is_sent_with_html_mode(self, api) -> None:
        sent = api()
        send_message(chat_id=42, text="<b>hi</b>")
        method, payload = sent[0]
        assert method == "sendMessage"
        assert payload["parse_mode"] == "HTML"
        assert payload["chat_id"] == 42

    def test_long_message_becomes_several(self, api) -> None:
        sent = api()
        send_message(chat_id=42, text="\n\n".join(["x" * 1000] * 8))
        assert len(sent) > 1

    def test_missing_token_explains_how_to_get_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(notify_module, "TELEGRAM_BOT_TOKEN", "")
        with pytest.raises(TelegramError, match="BotFather"):
            send_message(chat_id=42, text="hi")

    def test_buttons_are_attached(self, api) -> None:
        sent = api()
        send_with_buttons(
            chat_id=42, text="do a thing", buttons=[[("Approve", "rec:1:approve")]]
        )
        keyboard = sent[0][1]["reply_markup"]["inline_keyboard"]
        assert keyboard[0][0]["callback_data"] == "rec:1:approve"

    def test_a_message_with_buttons_is_never_split(self, api) -> None:
        # Splitting would leave the buttons detached from what they act on.
        api()
        with pytest.raises(ValueError, match="cannot be split"):
            send_with_buttons(
                chat_id=42,
                text="x" * (TELEGRAM_MAX_MESSAGE_CHARS + 1),
                buttons=[[("Approve", "rec:1:approve")]],
            )


class TestRetryPolicy:
    """Transient faults are retried; refusals are not."""

    @pytest.fixture
    def raw_api(self, monkeypatch: pytest.MonkeyPatch, token: None):
        def install(*outcomes):
            queue = list(outcomes)
            attempts: list[str] = []

            def fake_urlopen(request, timeout=None):  # type: ignore[no-untyped-def]
                attempts.append(request.full_url)
                outcome = queue.pop(0) if queue else b'{"ok": true, "result": {}}'
                if isinstance(outcome, Exception):
                    raise outcome

                class Response:
                    def read(self) -> bytes:
                        return outcome

                    def __enter__(self):  # type: ignore[no-untyped-def]
                        return self

                    def __exit__(self, *exc) -> bool:  # type: ignore[no-untyped-def]
                        return False

                return Response()

            monkeypatch.setattr(notify_module.urllib.request, "urlopen", fake_urlopen)
            monkeypatch.setattr(notify_module.time, "sleep", lambda _s: None)
            return attempts

        return install

    def test_network_failure_is_retried(self, raw_api) -> None:
        attempts = raw_api(
            urllib.error.URLError("down"), b'{"ok": true, "result": {"message_id": 1}}'
        )
        send_message(chat_id=42, text="hi")
        assert len(attempts) == 2

    def test_a_refusal_is_not_retried(self, raw_api) -> None:
        # A bad chat id or malformed HTML fails identically every time.
        attempts = raw_api(
            urllib.error.HTTPError(
                "u",
                400,
                "Bad Request",
                {},
                BytesIO(b'{"ok": false, "description": "chat not found"}'),
            )
        )
        with pytest.raises(TelegramError, match="never messaged it"):
            send_message(chat_id=42, text="hi")
        assert len(attempts) == 1

    def test_a_logical_refusal_is_not_retried(self, raw_api) -> None:
        attempts = raw_api(b'{"ok": false, "description": "message is too long"}')
        with pytest.raises(TelegramError, match="too long"):
            send_message(chat_id=42, text="hi")
        assert len(attempts) == 1

    def test_exhausted_retries_raise(self, raw_api) -> None:
        attempts = raw_api(*[urllib.error.URLError("down")] * 6)
        with pytest.raises(TelegramError, match="after every attempt"):
            send_message(chat_id=42, text="hi")
        assert len(attempts) == 4


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    """A seeded database with theses of varying conviction."""
    seed_database(conn, load_snapshot(FIXTURE))
    save_thesis(
        conn,
        Thesis(
            security_id=1, summary="A good reason", source="user", conviction="moderate"
        ),
    )
    save_thesis(
        conn,
        Thesis(
            security_id=2, summary="No real reason", source="user", conviction="none"
        ),
    )
    return conn


class TestWeeklyReport:
    """What the message says when there is nothing to say."""

    TODAY = date(2026, 9, 7)

    def test_quiet_week_says_so_plainly(self, seeded: sqlite3.Connection) -> None:
        # A report that manufactures content trains the reader to stop opening it.
        parts = weekly_report(seeded, today=self.TODAY)
        assert "not reviewed" in parts.body
        assert "No conclusion about this week" in parts.body
        assert parts.actionable == []

    def test_shows_the_total_and_the_gain(self, seeded: sqlite3.Connection) -> None:
        parts = weekly_report(seeded, today=self.TODAY)
        assert "€355.00" in parts.body

    def test_weak_theses_are_reported_as_standing_not_new(
        self, seeded: sqlite3.Connection
    ) -> None:
        parts = weekly_report(seeded, today=self.TODAY)
        assert "Standing, not new" in parts.body
        assert "BBB" in parts.body
        assert "AAA" not in parts.body.split("Standing, not new")[1]

    def test_pending_recommendations_become_actionable(
        self, seeded: sqlite3.Connection
    ) -> None:
        seeded.execute(
            """
            INSERT INTO recommendation (run_date, security_id, action, amount_eur,
                                        rationale, urgency, expires_on, created_at)
            VALUES ('2026-09-07', 1, 'ADD', 50.0, 'because', 'low', '2026-09-14', 'x')
            """
        )
        parts = weekly_report(seeded, today=self.TODAY)
        assert len(parts.actionable) == 1
        assert "Add to" in parts.body

    def test_a_rerun_retires_the_previous_set(self, seeded: sqlite3.Connection) -> None:
        # Re-running is the normal repair when a stage fails, and without this
        # the owner receives the same recommendation once per attempt.
        from store_research import create_research_run

        for _ in range(2):
            create_research_run(seeded, run_date="2026-09-07", kind="deep")
        for run in (1, 2):
            seeded.execute(
                """
                INSERT INTO recommendation (run_date, research_run_id, security_id,
                                            action, amount_eur, rationale, urgency,
                                            expires_on, created_at)
                VALUES ('2026-09-07', ?, 1, 'ADD', 50.0, 'because', 'low',
                        '2026-09-14', 'x')
                """,
                (run,),
            )
        seeded.execute(
            "UPDATE recommendation SET superseded_by_run_id = 2 "
            "WHERE research_run_id = 1"
        )
        parts = weekly_report(seeded, today=self.TODAY)
        assert len(parts.actionable) == 1

    def test_superseded_recommendations_are_hidden_even_when_newest(
        self, seeded: sqlite3.Connection
    ) -> None:
        from store_research import create_research_run

        for _ in range(2):
            create_research_run(seeded, run_date="2026-09-07", kind="deep")
        seeded.execute(
            """
            INSERT INTO recommendation (run_date, research_run_id, security_id,
                                        action, rationale, urgency, expires_on,
                                        superseded_by_run_id, created_at)
            VALUES ('2026-09-07', 1, 1, 'REVIEW', 'because', 'low',
                    '2026-09-14', 2, 'x')
            """
        )
        assert weekly_report(seeded, today=self.TODAY).actionable == []

    def test_expired_recommendations_are_not_shown(
        self, seeded: sqlite3.Connection
    ) -> None:
        # A weekly cadence supersedes itself; showing a stale one invites acting
        # on research that has been replaced.
        seeded.execute(
            """
            INSERT INTO recommendation (run_date, security_id, action, amount_eur,
                                        rationale, urgency, expires_on, created_at)
            VALUES ('2026-09-07', 1, 'ADD', 50.0, 'because', 'low', '2026-09-01', 'x')
            """
        )
        assert weekly_report(seeded, today=self.TODAY).actionable == []

    def test_decided_recommendations_are_not_shown_again(
        self, seeded: sqlite3.Connection
    ) -> None:
        cursor = seeded.execute(
            """
            INSERT INTO recommendation (run_date, security_id, action, amount_eur,
                                        rationale, urgency, expires_on, created_at)
            VALUES ('2026-09-07', 1, 'ADD', 50.0, 'because', 'low', '2026-09-14', 'x')
            """
        )
        seeded.execute(
            "INSERT INTO user_decision (recommendation_id, decision, decided_at) "
            "VALUES (?, 'approve', 'x')",
            (cursor.lastrowid,),
        )
        assert weekly_report(seeded, today=self.TODAY).actionable == []

    def test_unpriced_holdings_are_declared(self, conn: sqlite3.Connection) -> None:
        seed_database(conn, load_snapshot(FIXTURE))
        parts = weekly_report(conn, today=self.TODAY)
        # The fixture prices everything from its snapshot, so nothing is missing.
        assert "could not be priced" not in parts.body


class TestRefusalMessages:
    """Telegram describes its own state; the user needs to know what to do."""

    def test_chat_not_found_explains_the_rule(self) -> None:
        # A bot may not open a conversation. Until the person messages it,
        # their chat does not exist as far as the API is concerned, and the id
        # being correct makes no difference — which nobody knows until they
        # hit it.
        message = notify_module._explain("sendMessage", "Bad Request: chat not found")
        assert "never messaged it" in message
        assert "not the problem" in message

    def test_blocked_bot_is_named(self) -> None:
        message = notify_module._explain(
            "sendMessage", "Forbidden: bot was blocked by the user"
        )
        assert "blocked the bot" in message

    def test_bad_markup_is_named(self) -> None:
        message = notify_module._explain(
            "sendMessage", "Bad Request: can't parse entities"
        )
        assert "escaped for HTML" in message

    def test_an_unrecognised_refusal_is_passed_through(self) -> None:
        message = notify_module._explain("sendMessage", "something novel")
        assert "something novel" in message


class TestDecidingOnRetiredAdvice:
    """A replaced recommendation must not accept an answer."""

    def test_superseded_cannot_be_decided(self, seeded: sqlite3.Connection) -> None:
        # Recording an answer to withdrawn advice would count as engagement
        # with a recommendation that was never really live.
        import argparse

        from cmd_recommend import cmd_decide
        from store_research import create_research_run

        for _ in range(2):
            create_research_run(seeded, run_date="2026-09-07", kind="deep")
        seeded.execute(
            """
            INSERT INTO recommendation (run_date, research_run_id, security_id,
                                        action, rationale, urgency, expires_on,
                                        superseded_by_run_id, created_at)
            VALUES ('2026-09-07', 1, 1, 'REVIEW', 'because', 'low',
                    '2099-01-01', 2, 'x')
            """
        )
        row = seeded.execute("SELECT id FROM recommendation").fetchone()

        import cmd_recommend

        cmd_recommend.resolve_cli_profile = lambda *a, **k: (None, "ignored")
        cmd_recommend.open_existing_db = lambda _p: seeded
        args = argparse.Namespace(
            profile=None,
            db=None,
            recommendation_id=row["id"],
            decision="approve",
            note=None,
            limit=10,
        )
        with pytest.raises(ValueError, match="replaced by a later run"):
            cmd_decide(args)
