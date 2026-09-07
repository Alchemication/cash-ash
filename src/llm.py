"""Model call infrastructure: retry, fallback, truncation recovery, logging.

Handles the mechanics of calling a provider and recording what happened.
Prompt assembly and anything domain-specific belong elsewhere; this module
knows nothing about portfolios.

Public API:
    call_llm      -- call a model and return an LLMResult, logging the call
    LLMResult     -- response text plus the metadata needed to evaluate it
    LLMError      -- the call failed after every attempt

Example:
    from llm import call_llm

    result = call_llm(
        conn,
        feature="triage",
        messages=[{"role": "user", "content": "..."}],
    )
    print(result.text, result.cost_usd)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from config import (
    LLM_RETRY_DELAYS,
    LLM_TIMEOUT_S,
    MAX_TOKENS_DEFAULT,
    MIN_MAX_TOKENS,
    TRUNCATION_RETRY_MULTIPLIER,
)
from store_research import log_llm_call

logger = logging.getLogger(__name__)

_TRANSIENT_SIGNALS = (
    "overloaded",
    "rate limit",
    "rate_limit",
    "timeout",
    "timed out",
    "connection aborted",
    "connection reset",
    "connection refused",
    "temporarily unavailable",
    "service unavailable",
    "internal server error",
    "bad gateway",
    "502",
    "503",
    "504",
)
"""Substrings marking a failure worth retrying.

Matched against the message because providers wrap transport faults in their
own exception types, so the class says nothing useful about the cause.
"""


class LLMError(RuntimeError):
    """Raised when a model call fails after exhausting every attempt."""


@dataclass(frozen=True)
class LLMResult:
    """A model response and the metadata needed to evaluate it later.

    Attributes:
        text: The response content.
        reasoning: Reasoning the model emitted, when it exposes it separately.
            Stored because it is the only window into why a conclusion was
            reached, and it is never recoverable after the fact.
        model: The model actually used, which may not be the one requested if a
            fallback fired.
        requested_model: The model routing asked for.
        input_tokens: Prompt tokens reported by the provider.
        output_tokens: Completion tokens, reasoning included.
        cost_usd: Cost as computed by litellm, or None when unavailable.
        latency_s: Wall-clock seconds for the successful attempt.
        finish_reason: Why generation stopped.
        attempts: How many attempts were needed.
        llm_call_id: Row id in ``llm_call``, for ``llm-log --id``.
    """

    text: str
    model: str
    requested_model: str
    reasoning: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    latency_s: float | None = None
    finish_reason: str | None = None
    attempts: int = 1
    llm_call_id: int | None = None


def _is_transient(exc: Exception) -> bool:
    """Return True when *exc* looks worth retrying rather than giving up on."""
    message = str(exc).lower()
    return any(signal in message for signal in _TRANSIENT_SIGNALS)


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from a provider response of uncertain shape."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _response_cost(response: Any) -> float | None:
    """Return the cost of a response in USD, or None if it cannot be computed."""
    import litellm

    try:
        cost = litellm.completion_cost(completion_response=response)
    except Exception:  # noqa: BLE001 - cost is best-effort, never fatal
        return None
    try:
        value = float(cost)
    except (TypeError, ValueError):
        return None
    return None if value != value else value


def _extract(response: Any) -> tuple[str, str | None, str | None]:
    """Return ``(content, reasoning, finish_reason)`` from a response."""
    choice = response.choices[0]
    message = choice.message
    content = _field(message, "content") or ""
    reasoning = _field(message, "reasoning_content") or None
    return content, reasoning, _field(choice, "finish_reason")


def _was_truncated(finish_reason: str | None) -> bool:
    """Return True when the reply was cut off by the output budget.

    Any ``length`` finish counts, not only one that produced nothing. The first
    version of this fired solely on empty content, on the reasoning that a
    model which thinks until the budget is gone has produced no answer. That
    missed the more common case: it thinks for most of the budget, starts
    answering, and stops mid-sentence. For structured output the two are
    identical in effect — a half-written JSON object is exactly as unusable as
    no JSON object — and it took a live decision call, cut off 2,579 characters
    into its answer after 34,501 characters of reasoning, to make that obvious.

    Retrying a genuinely long prose answer that merely reached the cap is the
    cost of this, and it is small: one retry at a larger budget, once.
    """
    return finish_reason == "length"


def call_llm(
    conn: sqlite3.Connection | None,
    *,
    feature: str,
    messages: list[dict[str, str]],
    model: str | None = None,
    fallback_model: str | None = None,
    max_tokens: int = MAX_TOKENS_DEFAULT,
    temperature: float | None = None,
    prompt_version: str | None = None,
    trace_id: int | None = None,
    response_format: dict | None = None,
    timeout: float = LLM_TIMEOUT_S,
) -> LLMResult:
    """Call a model, retrying transient failures, and log the outcome.

    Attempts, in order: the routed model up to ``len(LLM_RETRY_DELAYS) + 1``
    times on transient errors, then the fallback model once if one is
    configured. A reply truncated before any content appeared is retried once
    with a larger budget, which is a different failure from a transient one and
    is not counted against the transient budget.

    Every attempt is logged, successful or not, because a model that fails
    repeatedly is exactly what a later evaluation needs to see.

    Args:
        conn: Database to log into, or None to skip logging.
        feature: Which part of the system is calling, for routing and analysis.
        messages: Chat messages in provider format.
        model: Model override; defaults to the feature's route.
        fallback_model: Model to try once if the primary keeps failing.
        max_tokens: Output budget, raised to ``MIN_MAX_TOKENS`` if below it.
        temperature: Sampling temperature, or None for the provider default.
        prompt_version: Identifier of the prompt used, stored for evaluation.
        trace_id: Groups this call with others in the same operation.
        response_format: Structured-output request, passed through.
        timeout: Per-request timeout in seconds.

    Returns:
        The result of the first successful attempt.

    Raises:
        LLMError: If every attempt failed.
    """
    import litellm

    from model_prefs import resolve_route

    route = resolve_route(feature)
    requested = model or route.model
    fallback = fallback_model if fallback_model is not None else route.fallback
    if temperature is None:
        temperature = route.temperature

    budget = max(int(max_tokens), MIN_MAX_TOKENS)
    if max_tokens < MIN_MAX_TOKENS:
        logger.debug(
            "Raised max_tokens from %d to the %d floor for %s",
            max_tokens,
            MIN_MAX_TOKENS,
            feature,
        )

    candidates: list[str] = [requested]
    if fallback and fallback != requested:
        candidates.append(fallback)

    attempt = 0
    last_error: Exception | None = None
    # Carried across candidates on purpose. If the primary needed a larger
    # budget to finish reasoning, the fallback almost certainly does too, and
    # retrying it at a size already known to be too small buys a guaranteed
    # failure at full price.
    current_budget = budget
    # One enlargement in total, not one per model. The first tells us the task
    # needs more room; trying the other model at that size tests whether a
    # terser one can finish. Enlarging again for each candidate compounds into
    # very large, very slow, very speculative calls.
    retried_for_truncation = False

    for candidate in candidates:
        transient_attempts = len(LLM_RETRY_DELAYS) + 1 if candidate == requested else 1
        index = 0

        while index < transient_attempts:
            attempt += 1
            kwargs: dict[str, Any] = {
                "model": candidate,
                "messages": messages,
                "max_tokens": current_budget,
                "timeout": timeout,
            }
            if temperature is not None:
                kwargs["temperature"] = temperature
            if response_format is not None:
                kwargs["response_format"] = response_format

            started = time.monotonic()
            try:
                response = litellm.completion(**kwargs)
            except Exception as exc:  # noqa: BLE001 - provider faults are opaque
                latency = time.monotonic() - started
                last_error = exc
                _log(
                    conn,
                    trace_id=trace_id,
                    feature=feature,
                    model=candidate,
                    requested_model=requested,
                    attempt=attempt,
                    prompt_version=prompt_version,
                    messages=messages,
                    max_tokens=current_budget,
                    temperature=temperature,
                    latency_s=latency,
                    error=f"{type(exc).__name__}: {exc}",
                )
                if not _is_transient(exc):
                    logger.warning("%s failed on %s: %s", feature, candidate, exc)
                    break
                if index < len(LLM_RETRY_DELAYS):
                    delay = LLM_RETRY_DELAYS[index]
                    logger.warning(
                        "%s transient failure on %s (%s); retrying in %ds",
                        feature,
                        candidate,
                        type(exc).__name__,
                        delay,
                    )
                    time.sleep(delay)
                index += 1
                continue

            latency = time.monotonic() - started
            content, reasoning, finish_reason = _extract(response)
            usage = _field(response, "usage")
            call_id = _log(
                conn,
                trace_id=trace_id,
                feature=feature,
                model=candidate,
                requested_model=requested,
                attempt=attempt,
                prompt_version=prompt_version,
                messages=messages,
                max_tokens=current_budget,
                temperature=temperature,
                latency_s=latency,
                response_text=content,
                reasoning_text=reasoning,
                finish_reason=finish_reason,
                input_tokens=_field(usage, "prompt_tokens"),
                output_tokens=_field(usage, "completion_tokens"),
                cost_usd=_response_cost(response),
            )

            if _was_truncated(finish_reason):
                if not retried_for_truncation:
                    retried_for_truncation = True
                    current_budget = int(current_budget * TRUNCATION_RETRY_MULTIPLIER)
                    logger.warning(
                        "%s on %s was cut off by its %d-token budget; retrying "
                        "with max_tokens=%d",
                        feature,
                        candidate,
                        int(current_budget / TRUNCATION_RETRY_MULTIPLIER),
                        current_budget,
                    )
                    continue
                # Escalate to the fallback rather than giving up: verbosity
                # differs between models, and a terser one can answer where a
                # discursive one never stops thinking. Observed in practice —
                # glm-4.7 answered a prompt glm-5.3-flash could not finish.
                # The enlarged budget goes with it.
                last_error = LLMError(
                    f"{candidate} was still cut off at {current_budget} tokens "
                    f"after a larger budget."
                )
                if candidate == candidates[-1]:
                    raise LLMError(
                        f"{feature}: every model was still cut off at "
                        f"{current_budget} tokens. The prompt is asking for "
                        f"more output than fits — shorten it or split the "
                        f"task, rather than raising the budget again."
                    ) from last_error
                break

            return LLMResult(
                text=content,
                reasoning=reasoning,
                model=candidate,
                requested_model=requested,
                input_tokens=_field(usage, "prompt_tokens"),
                output_tokens=_field(usage, "completion_tokens"),
                cost_usd=_response_cost(response),
                latency_s=latency,
                finish_reason=finish_reason,
                attempts=attempt,
                llm_call_id=call_id,
            )

        if candidate != candidates[-1]:
            logger.warning(
                "Falling back from %s to %s for %s", candidate, fallback, feature
            )

    raise LLMError(
        f"{feature}: every attempt failed across {', '.join(candidates)}. "
        f"Last error: {last_error}"
    ) from last_error


def _log(
    conn: sqlite3.Connection | None,
    *,
    trace_id: int | None,
    feature: str,
    model: str,
    requested_model: str,
    attempt: int,
    prompt_version: str | None,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float | None,
    latency_s: float,
    response_text: str | None = None,
    reasoning_text: str | None = None,
    finish_reason: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
    error: str | None = None,
) -> int | None:
    """Record one attempt, swallowing logging failures.

    A call that succeeded must not be turned into a failure because writing its
    log row did not work.
    """
    if conn is None:
        return None
    try:
        return log_llm_call(
            conn,
            trace_id=trace_id,
            feature=feature,
            model=model,
            requested_model=requested_model,
            attempt=attempt,
            prompt_version=prompt_version,
            messages_json=json.dumps(messages, ensure_ascii=False),
            response_text=response_text,
            reasoning_text=reasoning_text,
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            latency_s=latency_s,
            max_tokens=max_tokens,
            temperature=temperature,
            error=error,
        )
    except Exception:  # noqa: BLE001 - logging must never break a call
        logger.exception("Failed to log LLM call for %s", feature)
        return None
