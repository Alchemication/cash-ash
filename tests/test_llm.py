"""Tests for model calling, retry, fallback, truncation recovery and logging.

No test here reaches a provider. ``litellm.completion`` is replaced with a
scripted stand-in, which is the only way to exercise the failure paths that
matter — a real provider cannot be asked to be overloaded on demand.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

import llm as llm_module
from config import MIN_MAX_TOKENS
from llm import LLMError, _is_transient, _was_truncated_before_answering, call_llm
from store_research import create_llm_trace, llm_cost_summary, load_llm_calls


def _response(
    content: str = "answer",
    *,
    reasoning: str | None = None,
    finish_reason: str = "stop",
    prompt_tokens: int = 10,
    completion_tokens: int = 20,
) -> SimpleNamespace:
    """Build a provider response of the shape litellm returns."""
    message = SimpleNamespace(content=content, reasoning_content=reasoning)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
    )
    return SimpleNamespace(choices=[choice], usage=usage)


class Script:
    """A stand-in for litellm.completion that replays a scripted sequence."""

    def __init__(self, *outcomes: object) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        outcome = self._outcomes.pop(0) if self._outcomes else _response()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove backoff delays so retry paths run instantly."""
    monkeypatch.setattr(llm_module.time, "sleep", lambda _seconds: None)


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch, no_sleep: None):
    """Install a scripted completion and neutralise cost lookup."""

    def install(*outcomes: object) -> Script:
        import litellm

        script = Script(*outcomes)
        monkeypatch.setattr(litellm, "completion", script)
        monkeypatch.setattr(llm_module, "_response_cost", lambda _r: 0.0001)
        return script

    return install


class TestTransientClassification:
    """Retrying a refusal wastes money; not retrying a blip loses a run."""

    @pytest.mark.parametrize(
        "message",
        [
            "Provider overloaded, try again",
            "429 rate limit exceeded",
            "Read timed out",
            "503 Service Unavailable",
            "Connection reset by peer",
        ],
    )
    def test_transient_failures_are_recognised(self, message: str) -> None:
        assert _is_transient(RuntimeError(message)) is True

    @pytest.mark.parametrize(
        "message",
        [
            "Invalid API key",
            "model not found",
            "context length exceeded",
            "content filtered",
        ],
    )
    def test_permanent_failures_are_not_retried(self, message: str) -> None:
        assert _is_transient(RuntimeError(message)) is False


class TestTruncationDetection:
    """An always-reasoning model runs out of budget mid-thought."""

    def test_empty_content_after_reasoning_is_truncation(self) -> None:
        assert _was_truncated_before_answering("", "thinking hard", "length") is True

    def test_whitespace_only_content_counts_as_empty(self) -> None:
        assert _was_truncated_before_answering("  \n ", "thinking", "length") is True

    def test_content_present_is_not_truncation(self) -> None:
        assert _was_truncated_before_answering("answer", "thinking", "length") is False

    def test_natural_stop_is_not_truncation(self) -> None:
        assert _was_truncated_before_answering("", "thinking", "stop") is False

    def test_no_reasoning_is_not_this_failure(self) -> None:
        # An empty reply from a non-reasoning model is a different problem and
        # a bigger budget will not fix it.
        assert _was_truncated_before_answering("", None, "length") is False


class TestCallLLM:
    """The call path: success, retry, fallback, recovery, logging."""

    MESSAGES = [{"role": "user", "content": "hello"}]

    def test_successful_call_returns_content(
        self, conn: sqlite3.Connection, patched
    ) -> None:
        patched(_response("the answer", reasoning="because"))
        result = call_llm(conn, feature="triage", messages=self.MESSAGES)
        assert result.text == "the answer"
        assert result.reasoning == "because"
        assert result.attempts == 1
        assert result.input_tokens == 10

    def test_max_tokens_floor_is_enforced(
        self, conn: sqlite3.Connection, patched
    ) -> None:
        # A budget small enough to be consumed entirely by reasoning is a bug,
        # so callers are not permitted to set one.
        script = patched()
        call_llm(conn, feature="triage", messages=self.MESSAGES, max_tokens=1)
        assert script.calls[0]["max_tokens"] == MIN_MAX_TOKENS

    def test_transient_failure_is_retried(
        self, conn: sqlite3.Connection, patched
    ) -> None:
        script = patched(RuntimeError("overloaded"), _response("recovered"))
        result = call_llm(conn, feature="triage", messages=self.MESSAGES)
        assert result.text == "recovered"
        assert result.attempts == 2
        assert len(script.calls) == 2

    def test_permanent_failure_skips_to_fallback(
        self, conn: sqlite3.Connection, patched
    ) -> None:
        script = patched(RuntimeError("Invalid API key"), _response("from fallback"))
        result = call_llm(
            conn,
            feature="triage",
            messages=self.MESSAGES,
            model="model-a",
            fallback_model="model-b",
        )
        assert result.text == "from fallback"
        assert result.model == "model-b"
        assert result.requested_model == "model-a"
        assert [call["model"] for call in script.calls] == ["model-a", "model-b"]

    def test_truncated_reply_is_retried_with_a_bigger_budget(
        self, conn: sqlite3.Connection, patched
    ) -> None:
        script = patched(
            _response("", reasoning="thinking…", finish_reason="length"),
            _response("finally an answer"),
        )
        result = call_llm(
            conn, feature="analyst", messages=self.MESSAGES, max_tokens=2000
        )
        assert result.text == "finally an answer"
        assert script.calls[1]["max_tokens"] > script.calls[0]["max_tokens"]

    def test_truncation_is_retried_once_per_model(
        self, conn: sqlite3.Connection, patched
    ) -> None:
        truncated = _response("", reasoning="thinking…", finish_reason="length")
        script = patched(truncated, truncated, truncated, truncated)
        with pytest.raises(LLMError, match="more output than fits"):
            call_llm(
                conn,
                feature="analyst",
                messages=self.MESSAGES,
                model="model-a",
                fallback_model="model-b",
            )
        # Twice on the primary, once on the fallback at the enlarged budget.
        assert [call["model"] for call in script.calls] == [
            "model-a",
            "model-a",
            "model-b",
        ]

    def test_fallback_can_answer_where_the_primary_truncated(
        self, conn: sqlite3.Connection, patched
    ) -> None:
        # Observed live: a terser model answers a prompt a discursive one never
        # finishes thinking about. Blocking fallback here would turn a
        # recoverable call into a failure.
        truncated = _response("", reasoning="thinking…", finish_reason="length")
        script = patched(truncated, truncated, _response("terser model answered"))
        result = call_llm(
            conn,
            feature="analyst",
            messages=self.MESSAGES,
            model="model-a",
            fallback_model="model-b",
        )
        assert result.text == "terser model answered"
        assert result.model == "model-b"
        assert len(script.calls) == 3

    def test_enlarged_budget_carries_to_the_fallback(
        self, conn: sqlite3.Connection, patched
    ) -> None:
        # Retrying the fallback at a size already known to be too small buys a
        # guaranteed failure at full price.
        truncated = _response("", reasoning="thinking…", finish_reason="length")
        script = patched(truncated, truncated, _response("ok"))
        call_llm(
            conn,
            feature="analyst",
            messages=self.MESSAGES,
            max_tokens=2000,
            model="model-a",
            fallback_model="model-b",
        )
        assert script.calls[2]["max_tokens"] == script.calls[1]["max_tokens"]
        assert script.calls[2]["max_tokens"] > script.calls[0]["max_tokens"]

    def test_exhausted_attempts_raise(self, conn: sqlite3.Connection, patched) -> None:
        patched(*[RuntimeError("overloaded")] * 8)
        with pytest.raises(LLMError, match="every attempt failed"):
            call_llm(conn, feature="triage", messages=self.MESSAGES)

    def test_no_connection_still_returns(self, patched) -> None:
        patched(_response("fine"))
        assert call_llm(None, feature="triage", messages=self.MESSAGES).text == "fine"

    def test_logging_failure_does_not_break_the_call(
        self, conn: sqlite3.Connection, patched, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A call that succeeded must not become a failure because writing its
        # log row did not work.
        patched(_response("survived"))
        monkeypatch.setattr(
            llm_module,
            "log_llm_call",
            lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
        )
        assert (
            call_llm(conn, feature="triage", messages=self.MESSAGES).text == "survived"
        )


class TestCallLogging:
    """Everything needed to evaluate a model later is stored at call time."""

    MESSAGES = [{"role": "user", "content": "hello"}]

    def test_successful_call_is_recorded(
        self, conn: sqlite3.Connection, patched
    ) -> None:
        patched(_response("answer", reasoning="why"))
        result = call_llm(
            conn, feature="triage", messages=self.MESSAGES, prompt_version="v1"
        )
        (row,) = load_llm_calls(conn)
        assert row["id"] == result.llm_call_id
        assert row["feature"] == "triage"
        assert row["prompt_version"] == "v1"
        assert row["reasoning_text"] == "why"
        assert json.loads(row["messages_json"]) == self.MESSAGES

    def test_failed_attempts_are_recorded_too(
        self, conn: sqlite3.Connection, patched
    ) -> None:
        # A model that fails repeatedly is what an evaluation needs to see, and
        # it is invisible if only successes are kept.
        patched(RuntimeError("overloaded"), _response("recovered"))
        call_llm(conn, feature="triage", messages=self.MESSAGES)
        rows = load_llm_calls(conn)
        assert len(rows) == 2
        assert any(row["error"] and "overloaded" in row["error"] for row in rows)

    def test_calls_group_under_a_trace(self, conn: sqlite3.Connection, patched) -> None:
        patched(_response(), _response())
        trace = create_llm_trace(conn, operation="weekly_run", reference="AMD")
        for feature in ("triage", "analyst"):
            call_llm(conn, feature=feature, messages=self.MESSAGES, trace_id=trace)
        rows = load_llm_calls(conn, trace_id=trace)
        assert [row["feature"] for row in rows] == ["triage", "analyst"]

    def test_cost_summary_groups_by_feature(
        self, conn: sqlite3.Connection, patched
    ) -> None:
        patched(_response(), _response(), _response())
        call_llm(conn, feature="triage", messages=self.MESSAGES)
        call_llm(conn, feature="analyst", messages=self.MESSAGES)
        call_llm(conn, feature="analyst", messages=self.MESSAGES)
        summary = {row["feature"]: row["calls"] for row in llm_cost_summary(conn)}
        assert summary == {"triage": 1, "analyst": 2}

    def test_cost_summary_counts_failures(
        self, conn: sqlite3.Connection, patched
    ) -> None:
        patched(RuntimeError("overloaded"), _response())
        call_llm(conn, feature="triage", messages=self.MESSAGES)
        (row,) = llm_cost_summary(conn)
        assert row["failures"] == 1
        assert row["calls"] == 2

    def test_errors_only_filter(self, conn: sqlite3.Connection, patched) -> None:
        patched(RuntimeError("overloaded"), _response())
        call_llm(conn, feature="triage", messages=self.MESSAGES)
        rows = load_llm_calls(conn, errors_only=True)
        assert len(rows) == 1
        assert rows[0]["error"] is not None
