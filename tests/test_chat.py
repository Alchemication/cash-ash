"""Tests for the chat loop.

The model is faked. What is being tested is not whether a model answers well —
that is a prompt question — but whether the loop survives the four ways a
tool-calling model derails: repeating a call, circling on identical results,
never stopping, and emitting its own markup as prose. Each of those produced a
broken turn in zdrowskit before it was guarded.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

import chat as chat_module
from chat import ChatAnswer, ConversationBuffer, answer
from models import Account, CashFlow, Security, Trade
from store import ensure_account, insert_cash_flow, insert_trade, upsert_security


@dataclass
class FakeFunction:
    """The function half of a provider tool call."""

    name: str
    arguments: str


@dataclass
class FakeCall:
    """One tool call as a provider would return it."""

    id: str
    function: FakeFunction


@dataclass
class FakeResult:
    """Just the fields the loop reads off an LLMResult."""

    text: str = ""
    tool_calls: tuple = ()
    raw_message: dict | None = None
    llm_call_id: int | None = 1


def _call(name: str, **arguments) -> FakeCall:
    return FakeCall(
        id=f"c{abs(hash((name, json.dumps(arguments, sort_keys=True)))) % 997}",
        function=FakeFunction(name=name, arguments=json.dumps(arguments)),
    )


def _tool_turn(*calls: FakeCall) -> FakeResult:
    return FakeResult(
        tool_calls=calls, raw_message={"role": "assistant", "tool_calls": []}
    )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """A migrated database with one priced holding and some cash."""
    from db.migrations import apply_migrations

    path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    ensure_account(
        conn,
        Account(name="revolut", broker="Revolut", currency="EUR", sync_mode="manual"),
    )
    security = upsert_security(
        conn,
        Security(ticker="TEST", name="Test Corp", currency="EUR", feed_symbol="TEST"),
    )
    insert_cash_flow(
        conn, CashFlow(flow_date="2026-01-01", kind="CONTRIBUTION", amount_eur=300.0)
    )
    insert_trade(
        conn,
        Trade(
            security_id=security,
            trade_date="2026-01-02",
            side="BUY",
            quantity=2.0,
            amount_eur=100.0,
        ),
    )
    conn.execute(
        "INSERT INTO prices VALUES (?, '2026-09-01', 60.0, 'EUR', 'test', 'now')",
        (security,),
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def log_conn(db: Path):  # type: ignore[no-untyped-def]
    """A writable connection for the llm_call log, as the daemon would pass."""
    from store import open_existing_db

    conn = open_existing_db(db)
    yield conn
    conn.close()


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Script the model's replies in order and record what it was sent."""
    state: dict = {"replies": [], "sent": [], "tools_offered": []}

    def fake_call_llm(conn, **kwargs):  # type: ignore[no-untyped-def]
        state["sent"].append(kwargs["messages"])
        state["tools_offered"].append(kwargs.get("tools"))
        if not state["replies"]:
            return FakeResult(text="ran out of scripted replies")
        return state["replies"].pop(0)

    import llm as llm_module

    monkeypatch.setattr(llm_module, "call_llm", fake_call_llm)
    return state


def _answer(log_conn, db: Path, history=None) -> ChatAnswer:  # type: ignore[no-untyped-def]
    return answer(
        log_conn,
        db_path=db,
        history=history or [{"role": "user", "content": "how much cash?"}],
    )


class TestPlainAnswer:
    """The ordinary path: one tool call, then an answer."""

    def test_a_tool_result_reaches_the_next_call(
        self, log_conn, db: Path, model
    ) -> None:
        model["replies"] = [
            _tool_turn(_call("portfolio_snapshot")),
            FakeResult(text="You hold €200 of cash."),
        ]
        result = _answer(log_conn, db)
        assert result.text == "You hold €200 of cash."
        assert result.tools_called == ("portfolio_snapshot",)
        assert result.degraded is False
        # The second call must carry the tool's own text, or the model is
        # answering from nothing.
        second = model["sent"][1]
        assert second[-1]["role"] == "tool"
        assert "Cash: €200.00." in second[-1]["content"]

    def test_an_answer_with_no_tools_is_returned_as_is(
        self, log_conn, db: Path, model
    ) -> None:
        model["replies"] = [FakeResult(text="Ask me about a holding.")]
        result = _answer(log_conn, db)
        assert result.text == "Ask me about a holding."
        assert result.tools_called == ()
        assert result.iterations == 1

    def test_the_schema_is_in_the_system_prompt(
        self, log_conn, db: Path, model
    ) -> None:
        # Without it the model invents column names and spends a turn finding out.
        model["replies"] = [FakeResult(text="ok")]
        _answer(log_conn, db)
        system = model["sent"][0][0]["content"]
        assert system.startswith("You are answering")
        assert "v_cash_ledger(account_id, entry_date" in system

    def test_several_calls_in_one_turn_all_run(self, log_conn, db: Path, model) -> None:
        model["replies"] = [
            _tool_turn(
                _call("portfolio_snapshot"),
                _call("run_sql", query="SELECT ticker FROM v_trades"),
            ),
            FakeResult(text="done"),
        ]
        result = _answer(log_conn, db)
        assert result.tools_called == ("portfolio_snapshot", "run_sql")
        assert result.rows == ({"ticker": "TEST"},)

    def test_structured_rows_survive_to_the_answer(
        self, log_conn, db: Path, model
    ) -> None:
        # A chart is drawn from these, so they must outlive the tool message.
        model["replies"] = [
            _tool_turn(_call("run_sql", query="SELECT ticker FROM v_trades")),
            FakeResult(text="one holding"),
        ]
        assert _answer(log_conn, db).rows == ({"ticker": "TEST"},)


class TestGuards:
    """Each of these produced a broken turn before it was guarded."""

    def test_a_repeated_call_stops_the_loop_and_still_answers(
        self, log_conn, db: Path, model
    ) -> None:
        snapshot = _call("portfolio_snapshot")
        model["replies"] = [
            _tool_turn(snapshot),
            _tool_turn(_call("portfolio_snapshot")),  # identical arguments
            FakeResult(text="You hold one position."),
        ]
        result = _answer(log_conn, db)
        assert result.text == "You hold one position."
        assert result.degraded is True
        # The synthesis call is offered no tools at all, so it cannot loop again.
        assert model["tools_offered"][-1] is None

    def test_the_iteration_cap_ends_in_an_answer_not_silence(
        self, log_conn, db: Path, model
    ) -> None:
        from config import CHAT_MAX_TOOL_ITERATIONS

        # Distinct arguments each time, so only the cap can stop it.
        model["replies"] = [
            _tool_turn(_call("run_sql", query=f"SELECT {n} AS n"))
            for n in range(CHAT_MAX_TOOL_ITERATIONS)
        ] + [FakeResult(text="Here is what I found.")]
        result = _answer(log_conn, db)
        assert result.iterations == CHAT_MAX_TOOL_ITERATIONS
        assert result.text == "Here is what I found."
        assert result.degraded is True

    def test_identical_results_from_different_queries_stop_the_loop(
        self, log_conn, db: Path, model
    ) -> None:
        # Circling: the query text differs, the rows do not.
        model["replies"] = [
            _tool_turn(_call("run_sql", query="SELECT ticker FROM v_trades")),
            _tool_turn(_call("run_sql", query="SELECT ticker FROM v_trades WHERE 1")),
            FakeResult(text="Just TEST."),
        ]
        result = _answer(log_conn, db)
        assert result.degraded is True
        assert result.text == "Just TEST."

    @pytest.mark.parametrize(
        "leaked",
        [
            "<|tool_calls|>run_sql",
            '{"tool_calls": [{"name": "run_sql"}]}',
            "<tool_call>portfolio_snapshot</tool_call>",
            '<invoke name="run_sql">',
        ],
    )
    def test_leaked_tool_markup_is_never_sent(
        self, log_conn, db: Path, model, leaked: str
    ) -> None:
        model["replies"] = [FakeResult(text=leaked), FakeResult(text="You hold TEST.")]
        result = _answer(log_conn, db)
        assert result.text == "You hold TEST."
        assert result.degraded is True

    def test_markup_twice_falls_back_to_a_readable_sentence(
        self, log_conn, db: Path, model
    ) -> None:
        # Two synthesis attempts, both markup: the person still gets prose.
        model["replies"] = [FakeResult(text="<tool_call>x</tool_call>")] * 3
        result = _answer(log_conn, db)
        assert "could not turn that into a readable answer" in result.text
        assert result.degraded is True

    def test_an_empty_reply_is_synthesised_rather_than_sent(
        self, log_conn, db: Path, model
    ) -> None:
        model["replies"] = [
            _tool_turn(_call("portfolio_snapshot")),
            FakeResult(text="   "),
            FakeResult(text="You hold TEST and €200 cash."),
        ]
        result = _answer(log_conn, db)
        assert result.text == "You hold TEST and €200 cash."

    def test_synthesis_is_given_the_results_already_gathered(
        self, log_conn, db: Path, model
    ) -> None:
        # Otherwise the escape hatch answers from nothing and invents figures.
        model["replies"] = [
            _tool_turn(_call("portfolio_snapshot")),
            _tool_turn(_call("portfolio_snapshot")),
            FakeResult(text="answer"),
        ]
        _answer(log_conn, db)
        final = model["sent"][-1][-1]["content"]
        assert "Tool results" in final
        assert "Cash: €200.00." in final


class TestProposals:
    """A proposed write travels back unapplied; nothing here may change data."""

    def test_a_proposal_reaches_the_answer(self, log_conn, db: Path, model) -> None:
        model["replies"] = [
            _tool_turn(_call("propose_cash_flow", kind="CONTRIBUTION", amount_eur=160)),
            FakeResult(text="Put that up to confirm."),
        ]
        result = _answer(log_conn, db)
        assert len(result.proposals) == 1
        assert result.proposals[0].kind == "cash_flow"
        assert "€200.00 → €360.00" in result.proposals[0].summary

    def test_proposing_writes_nothing_by_itself(
        self, log_conn, db: Path, model
    ) -> None:
        # The whole point: a model cannot change the book, only ask to.
        from portfolio import cash_eur

        model["replies"] = [
            _tool_turn(_call("propose_cash_flow", kind="CONTRIBUTION", amount_eur=160)),
            FakeResult(text="Put that up to confirm."),
        ]
        _answer(log_conn, db)
        assert cash_eur(log_conn, account_id=1) == pytest.approx(200.0)
        assert log_conn.execute("SELECT COUNT(*) FROM pending_write").fetchone()[0] == 0

    def test_the_model_is_told_it_is_not_done(self, log_conn, db: Path, model) -> None:
        # Told only "proposed", a model reports back that it is recorded and the
        # owner stops looking for the button.
        model["replies"] = [
            _tool_turn(_call("propose_context_note", file="log", text="float holds")),
            FakeResult(text="Put that up to confirm."),
        ]
        _answer(log_conn, db)
        told = model["sent"][1][-1]["content"]
        assert "nothing is recorded yet" in told
        assert "Do not say it is done" in told

    def test_a_refused_proposal_is_a_readable_reason(
        self, log_conn, db: Path, model
    ) -> None:
        # The model has to hear what would be acceptable, not that it crashed.
        model["replies"] = [
            _tool_turn(
                _call(
                    "propose_cash_flow",
                    kind="CONTRIBUTION",
                    amount_eur=160,
                    new_balance_eur=250,
                )
            ),
            FakeResult(text="Which did you mean?"),
        ]
        result = _answer(log_conn, db)
        assert result.proposals == ()
        assert "not both and not neither" in model["sent"][1][-1]["content"]

    def test_a_read_only_turn_is_offered_no_writing_tools(
        self, log_conn, db: Path, model
    ) -> None:
        model["replies"] = [FakeResult(text="ok")]
        answer(
            log_conn,
            db_path=db,
            history=[{"role": "user", "content": "cash?"}],
            can_write=False,
        )
        offered = {tool["function"]["name"] for tool in model["tools_offered"][0]}
        assert offered == {"run_sql", "portfolio_snapshot", "concentration_report"}

    def test_a_writing_turn_offers_both_sets(self, log_conn, db: Path, model) -> None:
        model["replies"] = [FakeResult(text="ok")]
        _answer(log_conn, db)
        offered = {tool["function"]["name"] for tool in model["tools_offered"][0]}
        assert "propose_cash_flow" in offered
        assert "portfolio_snapshot" in offered


class TestToolFailures:
    """A tool that fails is a fact the model works around, not a crash."""

    def test_an_unknown_tool_does_not_end_the_turn(
        self, log_conn, db: Path, model
    ) -> None:
        model["replies"] = [
            _tool_turn(_call("sell_everything", ticker="TEST")),
            FakeResult(text="I cannot do that."),
        ]
        result = _answer(log_conn, db)
        assert result.text == "I cannot do that."
        assert "Error: No tool named sell_everything" in model["sent"][1][-1]["content"]

    def test_malformed_arguments_become_an_empty_call(
        self, log_conn, db: Path, model
    ) -> None:
        # The tool then says what it needed, which the model can act on.
        broken = FakeCall(
            id="c1", function=FakeFunction(name="run_sql", arguments="{{")
        )
        model["replies"] = [_tool_turn(broken), FakeResult(text="Let me rephrase.")]
        result = _answer(log_conn, db)
        assert result.text == "Let me rephrase."
        assert "Empty query" in model["sent"][1][-1]["content"]

    def test_a_refused_write_is_reported_to_the_model(
        self, log_conn, db: Path, model
    ) -> None:
        model["replies"] = [
            _tool_turn(_call("run_sql", query="DELETE FROM trades")),
            FakeResult(text="I can only read."),
        ]
        _answer(log_conn, db)
        assert "read-only" in model["sent"][1][-1]["content"]


class TestConversationBuffer:
    """Recent turns, capped, in memory."""

    def test_it_keeps_order_and_drops_the_oldest(self) -> None:
        buffer = ConversationBuffer(limit=3)
        for index in range(5):
            buffer.add("user", str(index))
        assert [message["content"] for message in buffer.messages()] == ["2", "3", "4"]

    def test_clearing_forgets_everything(self) -> None:
        buffer = ConversationBuffer()
        buffer.add("user", "hello")
        buffer.clear()
        assert len(buffer) == 0

    def test_history_is_passed_through_to_the_model(
        self, log_conn, db: Path, model
    ) -> None:
        model["replies"] = [FakeResult(text="ok")]
        buffer = ConversationBuffer()
        buffer.add("user", "what do I hold?")
        buffer.add("assistant", "TEST.")
        buffer.add("user", "and its cost?")
        _answer(log_conn, db, history=buffer.messages())
        sent = model["sent"][0]
        assert [message["content"] for message in sent[1:]] == [
            "what do I hold?",
            "TEST.",
            "and its cost?",
        ]


class TestLogging:
    """Every call is recorded, because chat is the easiest way to spend money."""

    def test_the_turn_is_logged_under_one_trace(
        self, log_conn, db: Path, model, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[int | None] = []

        def fake_call_llm(conn, **kwargs):  # type: ignore[no-untyped-def]
            seen.append(kwargs.get("trace_id"))
            return FakeResult(text="ok")

        import llm as llm_module

        monkeypatch.setattr(llm_module, "call_llm", fake_call_llm)
        _answer(log_conn, db)
        assert seen and seen[0] is not None
        row = log_conn.execute(
            "SELECT feature FROM llm_trace WHERE id = ?", (seen[0],)
        ).fetchone()
        assert row["feature"] == "chat"

    def test_chat_is_a_routable_feature(self) -> None:
        # So the model it uses can be changed and its cost seen, like any stage.
        from model_prefs import FEATURES, resolve_route

        assert "chat" in FEATURES
        assert resolve_route("chat").model


class TestPromptContract:
    """Rules the prompt has to keep carrying, taken from observed failures."""

    @pytest.fixture
    def prompt(self) -> str:
        # Whitespace-normalised: these rules are about what the prompt says, and
        # reflowing a paragraph must not fail a test about its meaning.
        from research import load_prompt

        return " ".join(load_prompt("chat.md").split())

    def test_a_question_is_not_an_instruction_to_write(self, prompt: str) -> None:
        # The failure this guards: a model proposing a log note because the
        # owner asked a question. It puts a button in front of someone who
        # wanted a number.
        assert "A question is not" in prompt
        assert "Only propose when they are telling you something to keep" in prompt

    def test_a_proposal_must_not_be_reported_as_done(self, prompt: str) -> None:
        assert "Never say it is recorded, added, saved or done" in prompt

    def test_the_model_must_not_ask_for_confirmation_in_words(
        self, prompt: str
    ) -> None:
        # Belt and braces with the buttons: asking as well reads as two
        # different confirmations and neither is obviously the real one.
        assert "The buttons do that" in prompt

    def test_a_note_must_not_restate_what_the_ledger_holds(self, prompt: str) -> None:
        # A note is replayed into later prompts; a weight written down today is
        # wrong within the week, and the ledger has the real one.
        assert "Never restate a value, weight, quantity or return" in prompt
        assert "without this conversation" in prompt

    def test_the_balance_ambiguity_is_called_out(self, prompt: str) -> None:
        assert "new_balance_eur" in prompt
        assert "ask" in prompt


def test_the_prompt_forbids_an_investment_recommendation() -> None:
    # The weekly pipeline has the guardrails; a chat window does not.
    from research import load_prompt

    prompt = load_prompt("chat.md")
    assert "You do not tell them what to buy or sell" in prompt
    assert "main.py recommend" in prompt
    assert "Never state a probability" in prompt
    assert chat_module.PROMPT_VERSION == "chat/1"
