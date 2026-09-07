"""Tests for the Telegram listener: routing, access, and not losing updates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import daemon as daemon_module
import notify as notify_module
from daemon import handle_update, run_daemon


@pytest.fixture
def roster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A roster with one enabled and one disabled profile."""
    import profiles as profiles_module

    db = tmp_path / "portfolio.db"
    db.touch()

    class FakeProfile:
        def __init__(self, name: str, telegram_id: int, enabled: bool = True) -> None:
            self.name = name
            self.telegram_id = telegram_id
            self.enabled = enabled
            self.db = db
            self.monthly_contribution_eur = 150.0

    profiles = {
        "adam": FakeProfile("adam", 111),
        "kasia": FakeProfile("kasia", 222, enabled=False),
    }
    monkeypatch.setattr(profiles_module, "load_profiles", lambda *a, **k: profiles)
    monkeypatch.setattr(daemon_module, "STATE_PATH", tmp_path / "state.json")
    return profiles


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch):
    """Capture outgoing Telegram calls instead of making them."""
    calls: dict[str, list] = {"answers": [], "messages": []}
    monkeypatch.setattr(
        daemon_module,
        "answer_callback",
        lambda **kw: calls["answers"].append(kw),
    )
    monkeypatch.setattr(
        daemon_module, "send_message", lambda **kw: calls["messages"].append(kw)
    )
    return calls


def _callback(sender: int, data: str) -> dict:
    return {
        "update_id": 1,
        "callback_query": {"id": "cb1", "from": {"id": sender}, "data": data},
    }


def _message(sender: int, text: str) -> dict:
    return {"update_id": 2, "message": {"from": {"id": sender}, "text": text}}


class TestAccessControl:
    """The bot's username is discoverable; the portfolio must not be."""

    def test_callback_from_an_unknown_id_is_ignored_silently(self, roster, spy) -> None:
        # Not answered at all: replying would confirm the bot is live and tell
        # a stranger their id is merely not on the list.
        result = handle_update(_callback(999, "rec:1:approve"))
        assert result.kind == "ignored"
        assert spy["answers"] == []

    def test_message_from_an_unknown_id_gets_no_reply(self, roster, spy) -> None:
        result = handle_update(_message(999, "/holdings"))
        assert result.kind == "ignored"
        assert spy["messages"] == []

    def test_a_disabled_profile_is_treated_as_unknown(self, roster, spy) -> None:
        result = handle_update(_message(222, "/holdings"))
        assert result.kind == "ignored"
        assert spy["messages"] == []


class TestCallbackParsing:
    """callback_data arrives from the network and reaches a database write."""

    @pytest.mark.parametrize(
        "data",
        [
            "rec:1:delete",
            "rec:abc:approve",
            "DROP TABLE recommendation",
            "rec:1:approve;rm -rf",
            "",
            "rec:1",
        ],
    )
    def test_malformed_payloads_are_refused(self, roster, spy, data: str) -> None:
        result = handle_update(_callback(111, data))
        assert result.kind == "bad_callback"
        assert spy["answers"][0]["text"] == "Unrecognised button."

    @pytest.mark.parametrize("decision", ["approve", "reject", "later"])
    def test_valid_payloads_are_accepted(
        self, roster, spy, monkeypatch: pytest.MonkeyPatch, decision: str
    ) -> None:
        monkeypatch.setattr(
            daemon_module, "_record_decision", lambda *a, **k: "Recorded."
        )
        result = handle_update(_callback(111, f"rec:7:{decision}"))
        assert result.kind == "decision"
        assert result.detail == decision


class TestOffsetHandling:
    """A stuck offset means nothing after it is ever seen again."""

    @pytest.fixture
    def polling(self, monkeypatch: pytest.MonkeyPatch, roster):
        def install(*batches):
            import config

            monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:abc")
            queue = list(batches)
            asked: list[int] = []

            def fake(*, offset: int, timeout: int):  # type: ignore[no-untyped-def]
                asked.append(offset)
                return queue.pop(0) if queue else []

            monkeypatch.setattr(daemon_module, "get_updates", fake)
            monkeypatch.setattr(daemon_module.time, "sleep", lambda _s: None)
            return asked

        return install

    def test_offset_advances_past_handled_updates(self, polling, spy) -> None:
        asked = polling(
            [{"update_id": 10, "message": {"from": {"id": 111}, "text": "/holdings"}}]
        )
        run_daemon(stop_after=2)
        assert asked == [0, 11]

    def test_offset_advances_past_an_update_that_crashed(
        self, polling, spy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A message that crashes the handler would crash it again on every
        # restart, wedging the daemon permanently.
        def boom(update):  # type: ignore[no-untyped-def]
            raise RuntimeError("bad update")

        monkeypatch.setattr(daemon_module, "handle_update", boom)
        asked = polling([{"update_id": 5, "message": {}}])
        run_daemon(stop_after=2)
        assert asked == [0, 6]

    def test_offset_survives_a_restart(self, polling, spy) -> None:
        polling(
            [{"update_id": 42, "message": {"from": {"id": 111}, "text": "/holdings"}}]
        )
        run_daemon(stop_after=1)
        assert json.loads(daemon_module.STATE_PATH.read_text())["offset"] == 43
        asked = polling()
        run_daemon(stop_after=1)
        assert asked == [43]

    def test_unreadable_state_starts_from_zero(self, polling, spy) -> None:
        daemon_module.STATE_PATH.write_text("not json")
        asked = polling()
        run_daemon(stop_after=1)
        assert asked == [0]

    def test_a_transient_poll_failure_is_retried(
        self, monkeypatch, roster, spy
    ) -> None:
        import config

        from notify import TelegramError

        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:abc")
        calls = {"n": 0}

        def flaky(*, offset: int, timeout: int):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] == 1:
                raise TelegramError("network down")
            return []

        monkeypatch.setattr(daemon_module, "get_updates", flaky)
        monkeypatch.setattr(daemon_module.time, "sleep", lambda _s: None)
        run_daemon(stop_after=2)
        assert calls["n"] == 2

    def test_a_second_poller_stops_the_daemon(self, monkeypatch, roster, spy) -> None:
        # Two pollers steal each other's updates, so button presses would be
        # handled at random. Retrying that forever would hide it.
        import config

        from notify import TelegramError

        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:abc")

        def conflict(*, offset: int, timeout: int):  # type: ignore[no-untyped-def]
            raise TelegramError("Another poller is already running for this bot.")

        monkeypatch.setattr(daemon_module, "get_updates", conflict)
        with pytest.raises(TelegramError, match="Another poller"):
            run_daemon(stop_after=3)


class TestUpdateShapes:
    """Telegram sends many kinds of update; most are not ours."""

    def test_an_unrecognised_update_is_skipped(self, roster, spy) -> None:
        assert handle_update({"update_id": 1, "poll": {}}).kind == "skipped"

    def test_an_empty_message_is_not_replied_to(self, roster, spy) -> None:
        assert handle_update(_message(111, "   ")).kind == "empty"
        assert spy["messages"] == []


class TestConflictDetection:
    """The 409 is worth naming, because its cause is not obvious."""

    def test_409_explains_the_cause(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.error
        from io import BytesIO

        from notify import TelegramError

        monkeypatch.setattr(notify_module, "TELEGRAM_BOT_TOKEN", "123:abc")

        def conflict(url, timeout=None):  # type: ignore[no-untyped-def]
            raise urllib.error.HTTPError("u", 409, "Conflict", {}, BytesIO(b"{}"))

        monkeypatch.setattr(notify_module.urllib.request, "urlopen", conflict)
        with pytest.raises(TelegramError, match="steal each other"):
            notify_module.get_updates(offset=0)


class TestMisconfiguration:
    """A permanent fault is not a flaky network."""

    def test_no_token_exits_rather_than_looping(
        self, roster, spy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The first version retried this every fifteen seconds forever, writing
        # the same warning each time.
        import config

        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setattr(
            daemon_module,
            "get_updates",
            lambda **kw: pytest.fail("should not have polled without a token"),
        )
        run_daemon(stop_after=5)


class TestLaunchdJob:
    """launchd starts jobs with almost nothing set."""

    def test_environment_carries_home(self) -> None:
        # Every user-owned path here derives from HOME, and an app home
        # resolved against a missing one lands somewhere unintended rather
        # than failing loudly.
        from cmd_daemon import _launchd_environment

        assert _launchd_environment()["HOME"]

    def test_path_includes_homebrew(self) -> None:
        # Absent from the default PATH entirely on Apple Silicon.
        from cmd_daemon import _launchd_environment

        assert "/opt/homebrew/bin" in _launchd_environment()["PATH"]

    def test_keepalive_does_not_restart_a_clean_exit(self) -> None:
        # An unconditional KeepAlive relaunches the daemon into the same
        # missing-token state every thirty seconds.
        from pathlib import Path as _Path

        from cmd_daemon import _render_plist

        plist = _render_plist(_Path("/tmp/project"))
        assert plist["KeepAlive"] == {"SuccessfulExit": False}

    def test_invoked_through_uv_not_a_fixed_interpreter(self) -> None:
        # A venv rebuilt or a dependency added must not leave the job pointing
        # at a stale interpreter that only fails at the next restart.
        from pathlib import Path as _Path

        from cmd_daemon import _render_plist

        args = _render_plist(_Path("/tmp/project"))["ProgramArguments"]
        assert args[0].endswith("uv")
        assert args[1:3] == ["run", "python"]


class TestWeeklySchedule:
    """One process schedules the week, keyed on the slot rather than the clock."""

    @pytest.fixture
    def state(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "state.json"
        monkeypatch.setattr(daemon_module, "STATE_PATH", path)
        return path

    @staticmethod
    def _seen(name: str = "adam", *, at=None) -> None:
        """Mark the profile as known, so the first-run guard does not apply.

        Defaults to the previous Sunday's slot, so a later check within the
        same week sees a slot that has genuinely already passed.
        """
        from datetime import datetime

        daemon_module.weekly_is_due(name, now=at or datetime(2026, 8, 30, 18))

    def test_a_new_profile_does_not_backfill_a_missed_slot(self, state) -> None:
        # Installing on a Saturday must not immediately fire the run that was
        # due last Sunday.
        from datetime import datetime

        assert (
            daemon_module.weekly_is_due("adam", now=datetime(2026, 9, 5, 20)) is False
        )

    def test_not_due_before_the_scheduled_hour(self, state) -> None:
        from datetime import datetime

        self._seen()
        assert daemon_module.weekly_is_due("adam", now=datetime(2026, 9, 6, 9)) is False

    def test_due_at_the_scheduled_hour(self, state) -> None:
        from datetime import datetime

        self._seen()
        assert daemon_module.weekly_is_due("adam", now=datetime(2026, 9, 6, 18)) is True

    def test_still_due_after_sleeping_through_the_slot(self, state) -> None:
        # The reason for keying on the slot: a machine asleep on Sunday evening
        # runs on waking rather than skipping the week.
        from datetime import datetime

        self._seen()
        assert daemon_module.weekly_is_due("adam", now=datetime(2026, 9, 7, 9)) is True

    def test_does_not_run_twice_the_next_morning(self, state) -> None:
        # ISO weeks start on Monday, so a Sunday run sits at the end of its
        # week and the next morning is a new one. Keying on the ISO week fired
        # a second run within a day.
        from datetime import datetime

        sunday = datetime(2026, 9, 6, 18)
        self._seen()
        daemon_module._record_weekly("adam", now=sunday)
        assert daemon_module.weekly_is_due("adam", now=sunday) is False
        assert daemon_module.weekly_is_due("adam", now=datetime(2026, 9, 7, 9)) is False
        assert (
            daemon_module.weekly_is_due("adam", now=datetime(2026, 9, 8, 10)) is False
        )

    def test_due_again_the_following_week(self, state) -> None:
        from datetime import datetime

        self._seen()
        daemon_module._record_weekly("adam", now=datetime(2026, 9, 6, 18))
        assert (
            daemon_module.weekly_is_due("adam", now=datetime(2026, 9, 13, 18)) is True
        )

    def test_profiles_are_tracked_separately(self, state) -> None:
        from datetime import datetime

        sunday = datetime(2026, 9, 6, 18)
        self._seen("adam")
        self._seen("kasia")
        daemon_module._record_weekly("adam", now=sunday)
        assert daemon_module.weekly_is_due("adam", now=sunday) is False
        assert daemon_module.weekly_is_due("kasia", now=sunday) is True

    def test_recording_a_run_does_not_lose_the_update_offset(self, state) -> None:
        # Both live in one state file; overwriting it here would replay the
        # Telegram backlog on the next poll.
        from datetime import datetime

        daemon_module._save_offset(4242)
        self._seen()
        daemon_module._record_weekly("adam", now=datetime(2026, 9, 6, 18))
        assert daemon_module._load_offset() == 4242

    def test_saving_an_offset_does_not_reschedule_the_week(self, state) -> None:
        from datetime import datetime

        sunday = datetime(2026, 9, 6, 18)
        self._seen()
        daemon_module._record_weekly("adam", now=sunday)
        daemon_module._save_offset(99)
        assert daemon_module.weekly_is_due("adam", now=sunday) is False
