"""The chat agent: one question in, one answer out, tools in between.

A turn is a loop. The model is given the portfolio's schema and three tools,
and keeps calling them until it has enough to answer. Most questions take one
or two calls; the cap exists for the ones that do not converge.

Everything here that looks defensive is. A tool-calling loop fails in ways a
single call does not, and each guard below is one of them:

- The model calls the same tool with the same arguments again, having already
  been given the answer. Repeating it cannot produce anything new, so the loop
  stops and makes it answer.
- Two different queries come back with identical rows, which means the model is
  circling rather than narrowing.
- It keeps wanting tools past the iteration cap.
- It emits its own tool-call syntax as prose — ``<|tool_calls|>``, a bare JSON
  function call — which is unreadable in Telegram and means the turn produced
  nothing.

All four end the same way: one final call with no tools at all, holding the
results already gathered, asked only to write the answer. That call is the
reason a turn can always produce something.

Public API:
    answer               -- run one chat turn to an answer
    ConversationBuffer   -- recent turns, for follow-up questions
    ChatAnswer           -- what a turn produced

Example:
    from chat import ConversationBuffer, answer

    buffer = ConversationBuffer()
    buffer.add("user", "how much cash do I have?")
    result = answer(conn, db_path=path, history=buffer.messages())
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from config import (
    CHAT_CONVERSATION_MESSAGES,
    CHAT_MAX_TOKENS,
    CHAT_MAX_TOOL_ITERATIONS,
)

logger = logging.getLogger(__name__)

PROMPT_VERSION = "chat/1"

_TOOL_MARKUP_MARKERS = (
    "<|tool_calls",
    "<｜tool_calls",
    "｜｜DSML",
    "tool_calls>",
    "<tool_call",
    "</tool_call",
    "<invoke name=",
    "<function_calls",
)
"""Fragments that mean a reply is internal syntax rather than an answer.

Matched on the way out because some models emit a tool call as text instead of
in the tool-call field, and the person would otherwise be shown machine markup
with no hint that anything went wrong.
"""

_SYNTHESIS_INSTRUCTION = (
    "Tool use is over for this turn. Answer the question now, in plain "
    "Telegram prose, using the tool results above as the only facts. Do not "
    "call a tool. Do not write SQL, JSON, XML or any function-call syntax. If "
    "the results do not answer the question, say exactly what is missing."
)

_MARKUP_RETRY_INSTRUCTION = (
    "Your previous reply was internal tool-call markup, not an answer. Write "
    "the answer as plain text for a person to read. No tool calls, no markup, "
    "no JSON."
)

_FALLBACK_TEXT = (
    "I could not turn that into a readable answer. Try asking it more "
    "narrowly — one holding, or one date range."
)


@dataclass(frozen=True)
class ChatAnswer:
    """What one chat turn produced.

    Attributes:
        text: The reply to send.
        rows: Structured rows from the last tool that returned a table, kept
            for anything that has to compute on them rather than read them.
        iterations: Tool-calling rounds used, for the log and for evals.
        tools_called: Tool names in the order they ran, same arguments counted
            once each.
        llm_call_id: The final call's row in ``llm_call``.
        degraded: True when a guard fired — the answer stands, but the loop did
            not finish the way it was supposed to.
    """

    text: str
    rows: tuple[dict, ...] = field(default_factory=tuple)
    iterations: int = 0
    tools_called: tuple[str, ...] = field(default_factory=tuple)
    llm_call_id: int | None = None
    degraded: bool = False


class ConversationBuffer:
    """Recent turns, so a follow-up question has something to refer back to.

    In memory and per profile, which is the right trade for something read on a
    phone: the cost of keeping it is paid on every call, since the whole buffer
    is resent, and a conversation worth resuming after a daemon restart has not
    happened yet. The portfolio itself is never remembered here — it is read
    from tools every turn, because it changes and the buffer does not.
    """

    def __init__(self, limit: int = CHAT_CONVERSATION_MESSAGES) -> None:
        """Create an empty buffer keeping at most *limit* messages."""
        self._limit = limit
        self._messages: list[dict[str, str]] = []

    def add(self, role: str, content: str) -> None:
        """Append one message, dropping the oldest past the limit."""
        self._messages.append({"role": role, "content": content})
        if len(self._messages) > self._limit:
            del self._messages[: len(self._messages) - self._limit]

    def messages(self) -> list[dict[str, str]]:
        """Return the buffered messages, oldest first."""
        return list(self._messages)

    def clear(self) -> None:
        """Forget the conversation."""
        self._messages.clear()

    def __len__(self) -> int:
        """Return how many messages are buffered."""
        return len(self._messages)


def _system_prompt(db_path: Path) -> str:
    """Build the system prompt: the instructions, plus the live schema."""
    from chat_tools import schema_summary
    from research import load_prompt

    return (
        f"{load_prompt('chat.md')}\n\n"
        f"## Database\n\n"
        f"Today is {date.today().isoformat()}. Money is stored in EUR; prices "
        f"are stored in each security's native currency, with the rate in "
        f"`fx_rates`. Tables and views, columns in order:\n\n"
        f"{schema_summary(db_path)}\n"
    )


def _call_signature(call: object) -> tuple[str, str]:
    """Return a stable identity for one tool call, for spotting a repeat."""
    function = getattr(call, "function", None)
    name = str(getattr(function, "name", ""))
    raw = getattr(function, "arguments", "")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return name, raw.strip()
    else:
        parsed = raw
    try:
        return name, json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    except TypeError:
        return name, str(parsed)


def _call_arguments(call: object) -> dict:
    """Return a tool call's arguments, tolerating whatever shape they arrive in.

    Malformed JSON becomes an empty dict rather than an exception: the tool then
    reports what it needed, which the model can act on, where a crash here ends
    the turn.
    """
    raw = getattr(getattr(call, "function", None), "arguments", "")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        logger.warning("Tool arguments were not JSON: %r", raw)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _looks_like_tool_markup(text: str) -> bool:
    """Return True when a reply is internal syntax rather than prose."""
    stripped = text.strip()
    if not stripped:
        return False
    if any(marker in stripped for marker in _TOOL_MARKUP_MARKERS):
        return True
    return stripped.startswith(("{", "[")) and "tool_call" in stripped


def answer(  # noqa: PLR0912, PLR0915 - the guards are the point
    conn,  # type: ignore[no-untyped-def]
    *,
    db_path: Path,
    history: list[dict[str, str]],
    trace_id: int | None = None,
) -> ChatAnswer:
    """Run one chat turn and return the reply to send.

    Args:
        conn: Open database connection, for logging model calls. Writes nothing
            else; the tools open their own read-only connection.
        db_path: Path to the profile's database, for the tools.
        history: The conversation so far, ending with the person's question.
        trace_id: Existing ``llm_trace`` row to attach calls to, or None to
            open one.

    Returns:
        The answer, and what it took to get there.

    Raises:
        LLMError: If the model could not be reached at all.
        ChatToolError: If the database cannot be read, which is not something
            the conversation can work around.
    """
    from chat_tools import chat_tools, execute_tool
    from llm import call_llm
    from store_research import create_llm_trace

    if trace_id is None:
        trace_id = create_llm_trace(conn, operation="chat", feature="chat")

    messages: list[dict] = [
        {"role": "system", "content": _system_prompt(db_path)},
        *history,
    ]
    tools = chat_tools()

    seen_calls: set[tuple[str, str]] = set()
    seen_results: set[str] = set()
    gathered: list[str] = []
    called: list[str] = []
    rows: tuple[dict, ...] = ()
    degraded = False
    iterations = 0
    result = None

    while iterations < CHAT_MAX_TOOL_ITERATIONS:
        iterations += 1
        result = call_llm(
            conn,
            feature="chat",
            messages=messages,
            tools=tools,
            max_tokens=CHAT_MAX_TOKENS,
            prompt_version=PROMPT_VERSION,
            trace_id=trace_id,
        )

        if not result.tool_calls:
            if _looks_like_tool_markup(result.text):
                logger.warning("Chat emitted tool markup as prose; synthesising")
                degraded = True
                break
            if not result.text.strip():
                # Nothing to call and nothing to say. Telegram refuses an empty
                # message, so this has to go through synthesis like any other
                # turn that produced no answer.
                logger.warning("Chat answered with nothing; synthesising")
                degraded = True
                break
            return ChatAnswer(
                text=result.text.strip(),
                rows=rows,
                iterations=iterations,
                tools_called=tuple(called),
                llm_call_id=result.llm_call_id,
            )

        repeats = [
            signature
            for signature in (_call_signature(call) for call in result.tool_calls)
            if signature in seen_calls
        ]
        if repeats:
            # It already has this answer. Asking again cannot change it, and
            # left alone the loop burns its whole budget on one query.
            logger.warning("Chat repeated a tool call; synthesising: %s", repeats)
            degraded = True
            break

        if result.raw_message is not None:
            messages.append(result.raw_message)

        for call in result.tool_calls:
            name = str(getattr(getattr(call, "function", None), "name", ""))
            arguments = _call_arguments(call)
            seen_calls.add(_call_signature(call))
            called.append(name)

            outcome = execute_tool(name, arguments, db_path)
            if outcome.rows:
                rows = outcome.rows
            gathered.append(
                f"### {name} {json.dumps(arguments, default=str)}\n{outcome.text}"
            )
            logger.info(
                "Chat tool %s -> %s",
                name,
                "ok" if outcome.ok else f"failed: {outcome.text[:120]}",
            )

            if outcome.ok and outcome.rows:
                fingerprint = outcome.text
                if fingerprint in seen_results:
                    # Different query, identical rows: it is circling.
                    logger.warning("Chat got an identical result again; synthesising")
                    degraded = True
                seen_results.add(fingerprint)

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": getattr(call, "id", "") or "",
                    "content": outcome.text,
                }
            )

        if degraded:
            break

    if result is None:  # pragma: no cover - the loop always runs once
        raise RuntimeError("Chat loop ended without calling a model.")

    needs_synthesis = (
        degraded
        or bool(result.tool_calls)
        or not result.text.strip()
        or _looks_like_tool_markup(result.text)
    )
    if not needs_synthesis:
        return ChatAnswer(
            text=result.text.strip(),
            rows=rows,
            iterations=iterations,
            tools_called=tuple(called),
            llm_call_id=result.llm_call_id,
        )

    logger.warning(
        "Chat forcing synthesis after %d iteration(s): wanted more tools=%s",
        iterations,
        bool(result.tool_calls),
    )
    final = _synthesise(
        conn,
        history=history,
        db_path=db_path,
        gathered=gathered,
        instruction=_SYNTHESIS_INSTRUCTION,
        trace_id=trace_id,
    )
    if _looks_like_tool_markup(final.text) or not final.text.strip():
        final = _synthesise(
            conn,
            history=history,
            db_path=db_path,
            gathered=gathered,
            instruction=_MARKUP_RETRY_INSTRUCTION,
            trace_id=trace_id,
        )

    text = final.text.strip()
    if _looks_like_tool_markup(text) or not text:
        logger.error("Chat could not produce prose after a retry; using fallback")
        text = _FALLBACK_TEXT

    return ChatAnswer(
        text=text,
        rows=rows,
        iterations=iterations,
        tools_called=tuple(called),
        llm_call_id=final.llm_call_id,
        degraded=True,
    )


def _synthesise(  # type: ignore[no-untyped-def]
    conn,
    *,
    history: list[dict[str, str]],
    db_path: Path,
    gathered: list[str],
    instruction: str,
    trace_id: int | None,
):
    """Ask for the answer with no tools available, holding what was gathered.

    The tool results are flattened into one message rather than replayed as a
    tool exchange, because the shape that got the model stuck is the shape being
    escaped. With no tools passed, it has nothing to call even if it wants to.
    """
    from llm import call_llm

    facts = "\n\n".join(gathered) if gathered else "No tool returned anything."
    return call_llm(
        conn,
        feature="chat",
        messages=[
            {"role": "system", "content": _system_prompt(db_path)},
            *history,
            {
                "role": "user",
                "content": f"{instruction}\n\n## Tool results\n\n{facts}",
            },
        ],
        max_tokens=CHAT_MAX_TOKENS,
        prompt_version=PROMPT_VERSION,
        trace_id=trace_id,
    )
