"""Tests for the Telegram side of a chat turn.

The model and Telegram are both faked. What matters here is the plumbing: a
question reaches the worker rather than the poller, the person sees something
immediately, the answer replaces it, and a failure still says something.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import daemon_chat as chat_plumbing


@pytest.fixture
def profile(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A profile whose database path exists but is never read here."""

    class FakeProfile:
        name = "adam"
        telegram_id = 111
        db = tmp_path / "portfolio.db"

    FakeProfile.db.touch()
    return FakeProfile()


@pytest.fixture
def telegram(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Record what would have been sent, in order."""
    calls: dict[str, list] = {"sent": [], "edited": []}

    import notify as notify_module

    def fake_send(*, chat_id: int, text: str, silent: bool = False) -> list[int]:
        calls["sent"].append(text)
        return [500 + len(calls["sent"])]

    def fake_edit(*, chat_id: int, message_id: int, text: str) -> None:
        calls["edited"].append((message_id, text))

    monkeypatch.setattr(notify_module, "send_message", fake_send)
    monkeypatch.setattr(notify_module, "edit_message", fake_edit)
    return calls


@pytest.fixture(autouse=True)
def clean_workers():  # type: ignore[no-untyped-def]
    """Leave no worker thread or conversation behind between tests."""
    yield
    chat_plumbing.shutdown()
    chat_plumbing._conversations.clear()


def _answers(monkeypatch: pytest.MonkeyPatch, text: str, **kwargs):  # type: ignore[no-untyped-def]
    """Make the chat loop return *text* without calling a model."""
    from chat import ChatAnswer

    seen: list[list[dict]] = []

    def fake_answer(conn, *, db_path, history, trace_id=None):  # type: ignore[no-untyped-def]
        seen.append(history)
        return ChatAnswer(text=text, **kwargs)

    import chat as chat_module

    monkeypatch.setattr(chat_module, "answer", fake_answer)
    return seen


class _NullConn:
    """Stands in for a database connection the fake loop never touches."""

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *args: object) -> bool:
        return False


@pytest.fixture
def db_open(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Stub the database open, since the loop itself is faked."""
    import store as store_module

    monkeypatch.setattr(store_module, "open_existing_db", lambda *a, **k: _NullConn())


class TestPlaceholder:
    """The person must see something before the model has finished."""

    def test_a_placeholder_is_sent_then_replaced(
        self, profile, telegram, db_open, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _answers(monkeypatch, "You hold €4.15 in cash.")
        chat_plumbing.run_chat_turn(profile, "how much cash?")
        assert telegram["sent"] == ["Working…"]
        assert telegram["edited"] == [(501, "You hold €4.15 in cash.")]

    def test_the_answer_is_escaped_for_telegram_html(
        self, profile, telegram, db_open, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A company name with an ampersand is enough for Telegram to reject the
        # whole message as broken HTML.
        _answers(monkeypatch, "You hold Smith & Co <test>.")
        chat_plumbing.run_chat_turn(profile, "what do I hold?")
        assert telegram["edited"][0][1] == "You hold Smith &amp; Co &lt;test&gt;."

    def test_a_failed_turn_still_says_something(
        self, profile, telegram, db_open, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import chat as chat_module

        def explode(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("provider down")

        monkeypatch.setattr(chat_module, "answer", explode)
        chat_plumbing.run_chat_turn(profile, "how much cash?")
        assert "Something went wrong" in telegram["edited"][0][1]

    def test_a_failed_edit_falls_back_to_a_new_message(
        self, profile, telegram, db_open, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Losing the answer because one edit failed would be the worse outcome.
        import notify as notify_module
        from notify import TelegramError

        def refuse(**kwargs):  # type: ignore[no-untyped-def]
            raise TelegramError("message to edit not found")

        monkeypatch.setattr(notify_module, "edit_message", refuse)
        _answers(monkeypatch, "Cash is €4.15.")
        chat_plumbing.run_chat_turn(profile, "cash?")
        assert telegram["sent"] == ["Working…", "Cash is €4.15."]


class TestConversation:
    """Follow-up questions need the turns before them."""

    def test_the_question_and_answer_are_remembered(
        self, profile, telegram, db_open, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _answers(monkeypatch, "BRK.B, at 19.4%.")
        chat_plumbing.run_chat_turn(profile, "largest position?")
        assert [
            message["content"]
            for message in chat_plumbing.conversation_for("adam").messages()
        ] == ["largest position?", "BRK.B, at 19.4%."]

    def test_history_reaches_the_loop_with_the_new_question_last(
        self, profile, telegram, db_open, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _answers(monkeypatch, "€265.31.")
        chat_plumbing.conversation_for("adam").add("user", "largest position?")
        chat_plumbing.conversation_for("adam").add("assistant", "BRK.B.")
        chat_plumbing.run_chat_turn(profile, "and its value?")
        assert [message["content"] for message in seen[0]] == [
            "largest position?",
            "BRK.B.",
            "and its value?",
        ]

    def test_a_failed_turn_is_not_remembered(
        self, profile, telegram, db_open, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Otherwise the next question is preceded by one that was never answered.
        import chat as chat_module

        monkeypatch.setattr(
            chat_module,
            "answer",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")),
        )
        chat_plumbing.run_chat_turn(profile, "cash?")
        assert len(chat_plumbing.conversation_for("adam")) == 0

    def test_reset_forgets_the_conversation(self, profile) -> None:
        chat_plumbing.conversation_for("adam").add("user", "hello")
        chat_plumbing.reset_conversation("adam")
        assert len(chat_plumbing.conversation_for("adam")) == 0


class TestBacklog:
    """Someone typing faster than the model answers."""

    def test_a_turn_is_queued_on_a_worker(
        self, profile, telegram, db_open, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _answers(monkeypatch, "ok")
        assert chat_plumbing.submit_chat_turn(profile, "cash?") == "queued"
        chat_plumbing.shutdown()
        assert telegram["edited"][0][1] == "ok"

    def test_past_the_backlog_a_turn_is_shed_with_a_message(
        self, profile, telegram, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Queueing indefinitely makes the person's own next reply later.
        monkeypatch.setitem(
            chat_plumbing._pending, profile.name, chat_plumbing.MAX_PENDING_TURNS
        )
        assert chat_plumbing.submit_chat_turn(profile, "cash?") == "shed"
        assert "send this one again" in telegram["sent"][0]

    def test_a_slot_is_released_even_when_the_turn_raises(
        self, profile, telegram, db_open, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A leaked slot would wedge the profile into permanent shedding.
        monkeypatch.setattr(
            chat_plumbing,
            "run_chat_turn",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        chat_plumbing.submit_chat_turn(profile, "cash?")
        chat_plumbing.shutdown()
        assert chat_plumbing._pending.get(profile.name, 0) == 0


class TestRouting:
    """Which path an incoming message takes."""

    def test_plain_words_go_to_the_chat_worker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import daemon as daemon_module

        queued: list[str] = []
        monkeypatch.setattr(
            daemon_module,
            "submit_chat_turn",
            lambda profile, text: queued.append(text) or "queued",
        )

        class FakeProfile:
            name = "adam"
            telegram_id = 111
            enabled = True
            db = Path("/tmp/nope.db")

        monkeypatch.setattr(
            daemon_module, "_authorised_profile", lambda *a: FakeProfile()
        )
        result = daemon_module.handle_update(
            {
                "update_id": 1,
                "message": {
                    "message_id": 2,
                    "from": {"id": 111},
                    "chat": {"id": 111, "type": "private"},
                    "text": "what did I pay for BRK.B?",
                },
            }
        )
        assert result.kind == "queued"
        assert queued == ["what did I pay for BRK.B?"]

    def test_a_slash_command_stays_on_the_polling_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Commands only read the database, so waiting for one costs nothing.
        import daemon as daemon_module

        monkeypatch.setattr(
            daemon_module,
            "submit_chat_turn",
            lambda *a: pytest.fail("a command must not reach the chat worker"),
        )
        monkeypatch.setattr(daemon_module, "_chat_reply", lambda profile, text: "rows")
        monkeypatch.setattr(daemon_module, "send_message", lambda **kw: [1])

        class FakeProfile:
            name = "adam"
            telegram_id = 111

        monkeypatch.setattr(
            daemon_module, "_authorised_profile", lambda *a: FakeProfile()
        )
        result = daemon_module.handle_update(
            {
                "update_id": 1,
                "message": {
                    "message_id": 2,
                    "from": {"id": 111},
                    "chat": {"id": 111, "type": "private"},
                    "text": "/holdings",
                },
            }
        )
        assert result.kind == "reply"
