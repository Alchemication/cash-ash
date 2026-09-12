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


class TestCharts:
    """A chart arrives as its own message; the prose never depends on it."""

    @pytest.fixture
    def photos(self, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
        """Record photos instead of sending them."""
        import notify as notify_module

        sent: list[tuple[int, str]] = []
        monkeypatch.setattr(
            notify_module,
            "send_photo",
            lambda *, chat_id, image, caption="": (
                sent.append((len(image), caption)) or 7
            ),
        )
        return sent

    @pytest.fixture
    def drawn(self, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
        """Render a fake PNG without starting a process."""
        import charts as charts_module

        calls: list[str] = []

        def fake_render(code, *, rows=()):  # type: ignore[no-untyped-def]
            calls.append(code)
            return b"\x89PNG-pretend"

        monkeypatch.setattr(charts_module, "render_chart", fake_render)
        return calls

    def test_a_chart_is_sent_and_removed_from_the_text(
        self, profile, telegram, db_open, photos, drawn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _answers(
            monkeypatch,
            'Weights below 20%.\n\n<chart title="Weights">\nfig = 1\n</chart>',
        )
        chat_plumbing.run_chat_turn(profile, "chart my weights")
        assert photos == [(len(b"\x89PNG-pretend"), "<b>Weights</b>")]
        assert telegram["edited"][0][1] == "Weights below 20%."

    def test_a_failed_chart_still_sends_the_prose(
        self, profile, telegram, db_open, photos, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The prompt forbids prose that depends on the picture, so losing the
        # picture must cost detail and not meaning.
        import charts as charts_module

        monkeypatch.setattr(charts_module, "render_chart", lambda *a, **k: None)
        monkeypatch.setattr(charts_module, "rows_chart", lambda *a, **k: None)
        _answers(monkeypatch, "BRK.B leads.\n\n<chart>\nfig = 1\n</chart>")
        chat_plumbing.run_chat_turn(profile, "chart my weights")
        assert photos == []
        assert telegram["edited"][0][1] == "BRK.B leads."
        assert "<chart>" not in telegram["edited"][0][1]

    def test_a_failed_chart_falls_back_to_the_rows(
        self, profile, telegram, db_open, photos, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import charts as charts_module

        monkeypatch.setattr(charts_module, "render_chart", lambda *a, **k: None)
        monkeypatch.setattr(charts_module, "rows_chart", lambda *a, **k: b"fallback")
        _answers(
            monkeypatch,
            "Weights.\n\n<chart>\nfig = 1\n</chart>",
            rows=({"ticker": "BRK.B", "weight_pct": 19.4},),
        )
        chat_plumbing.run_chat_turn(profile, "chart my weights")
        assert photos == [(len(b"fallback"), "")]

    def test_a_chart_nobody_asked_for_is_not_sent(
        self, profile, telegram, db_open, photos, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Rows are present, but the question wanted a number.
        _answers(
            monkeypatch,
            "You hold €4.15 in cash.",
            rows=({"ticker": "BRK.B", "weight_pct": 19.4},),
        )
        chat_plumbing.run_chat_turn(profile, "how much cash do I have")
        assert photos == []

    def test_a_requested_chart_the_model_forgot_is_drawn_from_the_rows(
        self, profile, telegram, db_open, photos, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import charts as charts_module

        monkeypatch.setattr(charts_module, "rows_chart", lambda *a, **k: b"fallback")
        _answers(
            monkeypatch,
            "BRK.B is 19.4%.",
            rows=({"ticker": "BRK.B", "weight_pct": 19.4},),
        )
        chat_plumbing.run_chat_turn(profile, "show me my weights")
        assert photos == [(len(b"fallback"), "")]

    def test_the_conversation_remembers_the_prose_not_the_chart_code(
        self, profile, telegram, db_open, photos, drawn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Chart code resent on every later turn would cost tokens forever.
        _answers(monkeypatch, "Weights.\n\n<chart>\nfig = 1\n</chart>")
        chat_plumbing.run_chat_turn(profile, "chart my weights")
        remembered = chat_plumbing.conversation_for("adam").messages()[-1]["content"]
        assert remembered == "Weights."


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


class TestProposalButtons:
    """A proposed write is persisted, then offered, then applied on a tap."""

    @pytest.fixture
    def real_db(self, tmp_path: Path):  # type: ignore[no-untyped-def]
        """A migrated database with cash, and a profile pointing at it."""
        import sqlite3

        from db.migrations import apply_migrations
        from models import Account, CashFlow
        from store import ensure_account, insert_cash_flow

        path = tmp_path / "portfolio.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        apply_migrations(conn)
        ensure_account(
            conn,
            Account(
                name="revolut", broker="Revolut", currency="EUR", sync_mode="manual"
            ),
        )
        insert_cash_flow(
            conn,
            CashFlow(flow_date="2026-01-01", kind="CONTRIBUTION", amount_eur=90.0),
        )
        conn.commit()
        conn.close()

        class RealProfile:
            name = "adam"
            telegram_id = 111
            db = path
            context = tmp_path / "context"

        return RealProfile()

    @pytest.fixture
    def buttons(self, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
        """Record button messages instead of sending them."""
        import notify as notify_module

        sent: list[tuple[str, list]] = []
        monkeypatch.setattr(
            notify_module,
            "send_with_buttons",
            lambda *, chat_id, text, buttons: sent.append((text, buttons)) or 9,
        )
        return sent

    def test_a_proposal_is_stored_and_offered_with_two_buttons(
        self, real_db, telegram, buttons, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from chat import ChatAnswer
        from proposals import cash_flow
        from store import open_existing_db

        with open_existing_db(real_db.db) as conn:
            proposal = cash_flow(conn, kind="CONTRIBUTION", new_balance_eur=250.0)

        import chat as chat_module

        monkeypatch.setattr(
            chat_module,
            "answer",
            lambda *a, **k: ChatAnswer(
                text="Put that up to confirm.", proposals=(proposal,)
            ),
        )
        chat_plumbing.run_chat_turn(real_db, "topped up to 250")

        text, rows = buttons[0]
        assert "€90.00 → €250.00" in text
        assert [label for label, _ in rows[0]] == ["Confirm", "Cancel"]
        with open_existing_db(real_db.db) as conn:
            stored = conn.execute(
                "SELECT id, resolved_at FROM pending_write"
            ).fetchall()
        assert len(stored) == 1
        assert stored[0]["resolved_at"] is None
        # Stored, but not applied: the tap is what applies it.
        from portfolio import cash_eur

        with open_existing_db(real_db.db) as conn:
            assert cash_eur(conn, account_id=1) == pytest.approx(90.0)

    def test_confirming_applies_it(self, real_db, buttons) -> None:
        from portfolio import cash_eur
        from proposals import cash_flow, save
        from store import open_existing_db

        with open_existing_db(real_db.db) as conn:
            proposal_id = save(
                conn, cash_flow(conn, kind="CONTRIBUTION", new_balance_eur=250.0)
            )
        message = chat_plumbing.resolve_proposal(real_db, proposal_id, "ok")
        assert "€250.00" in message
        with open_existing_db(real_db.db) as conn:
            assert cash_eur(conn, account_id=1) == pytest.approx(250.0)

    def test_cancelling_applies_nothing(self, real_db, buttons) -> None:
        from portfolio import cash_eur
        from proposals import cash_flow, save
        from store import open_existing_db

        with open_existing_db(real_db.db) as conn:
            proposal_id = save(
                conn, cash_flow(conn, kind="CONTRIBUTION", amount_eur=10.0)
            )
        assert "Nothing was recorded" in chat_plumbing.resolve_proposal(
            real_db, proposal_id, "no"
        )
        with open_existing_db(real_db.db) as conn:
            assert cash_eur(conn, account_id=1) == pytest.approx(90.0)

    def test_a_note_reaches_the_profile_s_context_directory(
        self, real_db, buttons
    ) -> None:
        from proposals import context_note, save
        from store import open_existing_db

        with open_existing_db(real_db.db) as conn:
            proposal_id = save(conn, context_note(file="log", text="float holds"))
        chat_plumbing.resolve_proposal(real_db, proposal_id, "ok")
        assert "float holds" in (real_db.context / "log.md").read_text()

    def test_a_confirmed_trade_moves_the_position(self, real_db, buttons) -> None:
        from models import Security, Trade
        from portfolio import positions
        from proposals import save, trade
        from store import insert_trade, open_existing_db, upsert_security

        with open_existing_db(real_db.db) as conn:
            security_id = upsert_security(
                conn,
                Security(
                    ticker="TEST", name="Test Corp", currency="EUR", feed_symbol="TEST"
                ),
            )
            insert_trade(
                conn,
                Trade(
                    security_id=security_id,
                    trade_date="2026-01-02",
                    side="BUY",
                    quantity=2.0,
                    amount_eur=20.0,
                ),
            )
            proposal_id = save(
                conn,
                trade(conn, ticker="TEST", side="BUY", quantity=1.0, amount_eur=10.0),
            )
        message = chat_plumbing.resolve_proposal(real_db, proposal_id, "ok")
        assert "Now holding 3 units" in message
        with open_existing_db(real_db.db) as conn:
            (position,) = positions(conn, account_id=1)
        assert position.quantity == pytest.approx(3.0)

    def test_an_unknown_proposal_says_so_without_raising(
        self, real_db, buttons
    ) -> None:
        # The toast has to say something; a traceback would show as silence.
        assert "gone" in chat_plumbing.resolve_proposal(real_db, 9999, "ok")

    def test_buttons_failing_to_send_leaves_the_proposal_recoverable(
        self, real_db, telegram, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stored first on purpose: a button naming a row that was never written
        # is worse than a row whose buttons can be re-offered.
        import notify as notify_module
        from chat import ChatAnswer
        from notify import TelegramError
        from proposals import cash_flow
        from store import open_existing_db

        monkeypatch.setattr(
            notify_module,
            "send_with_buttons",
            lambda **kw: (_ for _ in ()).throw(TelegramError("too long")),
        )
        with open_existing_db(real_db.db) as conn:
            proposal = cash_flow(conn, kind="CONTRIBUTION", amount_eur=10.0)
        import chat as chat_module

        monkeypatch.setattr(
            chat_module,
            "answer",
            lambda *a, **k: ChatAnswer(text="ok", proposals=(proposal,)),
        )
        chat_plumbing.run_chat_turn(real_db, "added 10")
        with open_existing_db(real_db.db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM pending_write").fetchone()[0] == 1


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
