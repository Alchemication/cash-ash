"""Broker refresh: sign-in relay, status mapping, and its Telegram messages."""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

import daemon_refresh
from broker_refresh import RefreshResult, run_broker_refresh
from revolut_download import SignInRequired
from revolut_login import SignInOutcome


class StubPage:
    """A page already at the portfolio, so the relay signs in at once."""

    url = "https://invest.revolut.com/home"

    def get_by_role(self, role: str, *, name: str, exact: bool):
        class Loc:
            async def is_visible(self) -> bool:
                return name == "Open profile"

        return Loc()

    async def screenshot(self) -> bytes:  # pragma: no cover - never reached signed in
        return b""


def _statement() -> SimpleNamespace:
    return SimpleNamespace(period_start=date(2026, 9, 1), period_end=date(2026, 9, 30))


def _profile(tmp_path) -> SimpleNamespace:
    db = tmp_path / "portfolio.db"
    db.touch()
    return SimpleNamespace(name="adam", telegram_id=111, db=db)


class TestRunBrokerRefresh:
    def test_reconciled_reports_row_counts_and_confirms_sign_in(self, tmp_path) -> None:
        signed_in: list[bool] = []

        async def deliver(_link: str) -> None:  # pragma: no cover - portfolio is up
            raise AssertionError("no QR needed when already signed in")

        async def download(profile, *, start, end, sign_in):
            # Exercise the real relay + on_signed_in wiring through a stub page.
            outcome = await sign_in(StubPage())
            assert outcome.ok
            return _statement(), tmp_path / "archive"

        def reconcile_fn(conn, statement):
            return {
                "rows": [
                    {"status": "quantity_match"},
                    {"status": "quantity_difference"},
                ]
            }

        result = asyncio.run(
            run_broker_refresh(
                _profile(tmp_path),
                deliver_link=deliver,
                on_signed_in=lambda: _record(signed_in),
                download=download,
                reconcile_fn=reconcile_fn,
            )
        )
        assert result.status == "reconciled" and result.ok
        assert "1 of 2 rows differ" in result.detail
        assert signed_in == [True]

    @pytest.mark.parametrize(
        "outcome_status,expected",
        [("timeout", "not_approved"), ("needs_attention", "sign_in_needed")],
    )
    def test_sign_in_failures_map_to_status(
        self, tmp_path, outcome_status: str, expected: str
    ) -> None:
        async def download(profile, *, start, end, sign_in):
            raise SignInRequired(SignInOutcome(outcome_status, "reason"))

        result = asyncio.run(
            run_broker_refresh(
                _profile(tmp_path),
                deliver_link=_noop,
                download=download,
                reconcile_fn=lambda *a: {"rows": []},
            )
        )
        assert result.status == expected and not result.ok
        assert result.detail == "reason"

    def test_download_error_is_failed(self, tmp_path) -> None:
        async def download(profile, *, start, end, sign_in):
            raise ValueError("No completed PDF download.")

        result = asyncio.run(
            run_broker_refresh(
                _profile(tmp_path),
                deliver_link=_noop,
                download=download,
                reconcile_fn=lambda *a: {"rows": []},
            )
        )
        assert result.status == "failed"
        assert "No completed PDF" in result.detail


async def _record(sink: list) -> None:
    sink.append(True)


async def _noop(_link: str) -> None:
    return None


class TestRefreshMessages:
    @pytest.mark.parametrize(
        "status,marker",
        [
            ("reconciled", "✅"),
            ("sign_in_needed", "⚠️"),
            ("not_approved", "⌛"),
            ("failed", "❌"),
        ],
    )
    def test_each_status_has_its_own_message(self, status: str, marker: str) -> None:
        text = daemon_refresh._result_text(RefreshResult(status, "detail & more <x>"))
        assert marker in text
        # Detail is HTML-escaped so a ticker or ampersand cannot break the message.
        if status in {"reconciled", "sign_in_needed", "failed"}:
            assert "&amp;" in text and "&lt;x&gt;" in text

    def test_login_link_url_is_escaped_in_the_href(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent: list[dict] = []
        monkeypatch.setattr(
            daemon_refresh,
            "send_message",
            lambda **kw: sent.append(kw),
            raising=False,
        )
        import notify

        monkeypatch.setattr(notify, "send_message", lambda **kw: sent.append(kw))
        daemon_refresh._send_login_link(
            SimpleNamespace(name="adam", telegram_id=111),
            'https://revolut.com/app?token=a"b&c',
        )
        body = sent[0]["text"]
        assert 'href="https://revolut.com/app?token=a&quot;b&amp;c"' in body

    def test_offer_sends_only_a_run_now_button(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No Snooze: nothing schedules a proposal, so there is nothing to defer.
        captured: dict = {}
        import notify

        monkeypatch.setattr(
            notify, "send_with_buttons", lambda **kw: captured.update(kw) or 1
        )
        daemon_refresh.offer_broker_refresh(
            SimpleNamespace(name="adam", telegram_id=111)
        )
        assert captured["buttons"] == [[("Run now", "refresh:run")]]


class TestConcurrency:
    """Chrome's session is one browser; two refreshes would fight over it."""

    def test_busy_profile_refuses_a_second_refresh(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent: list[dict] = []
        import notify

        monkeypatch.setattr(notify, "send_message", lambda **kw: sent.append(kw))
        profile = SimpleNamespace(name="adam", telegram_id=111, db=tmp_path / "p.db")
        with daemon_refresh._lock:
            daemon_refresh._running.add("adam")
        try:
            assert daemon_refresh.submit_broker_refresh(profile) == "busy"
        finally:
            with daemon_refresh._lock:
                daemon_refresh._running.discard("adam")
        # Says nothing itself: the caller phrases it, so one tap is acknowledged once.
        assert sent == []
