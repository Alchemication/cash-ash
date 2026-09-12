"""Tests for proposed writes.

Everything here computes money or decides what gets written to a file the
research pipeline reads, so the cases that matter are the ones where a sentence
was misread: "to 250" taken as "by 250", a kind that contradicts the direction
cash moved, a proposal confirmed long after the balance it was built against.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from models import Account, CashFlow
from portfolio import cash_eur
from proposals import (
    ProposalError,
    cancel,
    cash_flow,
    confirm,
    context_note,
    expire_stale,
    load,
    save,
    trade,
)
from store import ensure_account, insert_cash_flow


@pytest.fixture
def funded(conn: sqlite3.Connection, account_id: int) -> sqlite3.Connection:
    """A book holding exactly €90.00 in cash."""
    insert_cash_flow(
        conn, CashFlow(flow_date="2026-01-01", kind="CONTRIBUTION", amount_eur=90.0)
    )
    return conn


class TestCashFlowFromADelta:
    """ "I put in 160"."""

    def test_a_contribution_adds_and_states_both_balances(
        self, funded: sqlite3.Connection
    ) -> None:
        proposal = cash_flow(funded, kind="CONTRIBUTION", amount_eur=160.0)
        assert proposal.payload["amount_eur"] == pytest.approx(160.0)
        assert "€90.00 → €250.00" in proposal.summary

    @pytest.mark.parametrize("kind", ["WITHDRAWAL", "FEE"])
    def test_money_leaving_is_stored_negative(
        self, funded: sqlite3.Connection, kind: str
    ) -> None:
        # The ledger sums a single signed column, so the sign is the whole
        # meaning of the row.
        proposal = cash_flow(funded, kind=kind, amount_eur=30.0)
        assert proposal.payload["amount_eur"] == pytest.approx(-30.0)
        assert "€90.00 → €60.00" in proposal.summary

    def test_a_negative_amount_is_read_as_a_magnitude(
        self, funded: sqlite3.Connection
    ) -> None:
        # Models pass -30 for a withdrawal about as often as 30; both mean the
        # same thing, and the kind already carries the direction.
        assert cash_flow(funded, kind="WITHDRAWAL", amount_eur=-30.0).payload[
            "amount_eur"
        ] == pytest.approx(-30.0)

    def test_a_dividend_adds(self, funded: sqlite3.Connection) -> None:
        proposal = cash_flow(funded, kind="DIVIDEND", amount_eur=1.2)
        assert proposal.payload["amount_eur"] == pytest.approx(1.2)


class TestCashFlowFromABalance:
    """ "I topped up to 250" — the reading that costs money if taken as a delta."""

    def test_a_target_balance_becomes_the_difference(
        self, funded: sqlite3.Connection
    ) -> None:
        proposal = cash_flow(funded, kind="CONTRIBUTION", new_balance_eur=250.0)
        assert proposal.payload["amount_eur"] == pytest.approx(160.0)
        assert "€160.00" in proposal.summary
        assert "€90.00 → €250.00" in proposal.summary

    def test_a_lower_target_is_a_withdrawal_not_a_contribution(
        self, funded: sqlite3.Connection
    ) -> None:
        # Cash falling cannot be a contribution, whatever the sentence said.
        with pytest.raises(ProposalError, match="does not match CONTRIBUTION"):
            cash_flow(funded, kind="CONTRIBUTION", new_balance_eur=50.0)
        assert cash_flow(funded, kind="WITHDRAWAL", new_balance_eur=50.0).payload[
            "amount_eur"
        ] == pytest.approx(-40.0)

    def test_a_target_equal_to_the_balance_is_refused(
        self, funded: sqlite3.Connection
    ) -> None:
        with pytest.raises(ProposalError, match="nothing to record"):
            cash_flow(funded, kind="CONTRIBUTION", new_balance_eur=90.0)

    def test_both_arguments_together_are_refused(
        self, funded: sqlite3.Connection
    ) -> None:
        # Ambiguity must not be resolved by precedence. It is asked about.
        with pytest.raises(ProposalError, match="not both and not neither"):
            cash_flow(
                funded, kind="CONTRIBUTION", amount_eur=160.0, new_balance_eur=250.0
            )

    def test_neither_argument_is_refused(self, funded: sqlite3.Connection) -> None:
        with pytest.raises(ProposalError, match="not both and not neither"):
            cash_flow(funded, kind="CONTRIBUTION")


class TestCashFlowValidation:
    """What a proposal refuses to carry."""

    def test_an_unknown_kind_lists_the_real_ones(
        self, funded: sqlite3.Connection
    ) -> None:
        with pytest.raises(ProposalError, match="CONTRIBUTION"):
            cash_flow(funded, kind="TRANSFER", amount_eur=10.0)

    def test_an_opening_balance_cannot_be_proposed(
        self, funded: sqlite3.Connection
    ) -> None:
        # It means "everything before this point" and the seed writes it once;
        # a second would double the book's history.
        with pytest.raises(ProposalError):
            cash_flow(funded, kind="OPENING_BALANCE", amount_eur=10.0)

    def test_a_future_date_is_refused(self, funded: sqlite3.Connection) -> None:
        ahead = (date.today() + timedelta(days=3)).isoformat()
        with pytest.raises(ProposalError, match="in the future"):
            cash_flow(funded, kind="CONTRIBUTION", amount_eur=10.0, day=ahead)

    def test_an_unparseable_date_says_the_format(
        self, funded: sqlite3.Connection
    ) -> None:
        with pytest.raises(ProposalError, match="YYYY-MM-DD"):
            cash_flow(funded, kind="CONTRIBUTION", amount_eur=10.0, day="last Tuesday")

    @pytest.mark.parametrize("amount", [0, 0.001, "lots", None, float("inf")])
    def test_an_unusable_amount_is_refused(
        self, funded: sqlite3.Connection, amount: object
    ) -> None:
        with pytest.raises(ProposalError):
            cash_flow(funded, kind="CONTRIBUTION", amount_eur=amount)  # type: ignore[arg-type]


@pytest.fixture
def invested(funded: sqlite3.Connection, security_id: int) -> sqlite3.Connection:
    """€90 cash and 4 units of TEST, a USD listing, bought for €40."""
    from models import Trade
    from store import insert_trade

    insert_trade(
        funded,
        Trade(
            security_id=security_id,
            trade_date="2026-01-02",
            side="BUY",
            quantity=4.0,
            amount_eur=40.0,
        ),
    )
    funded.execute(
        "INSERT INTO fx_rates VALUES ('2026-09-01', 'USD', 'EUR', 0.9, 'test', 'now')"
    )
    return funded


class TestTradeFromAnAmount:
    """ "I bought 2 more for €30"."""

    def test_a_buy_states_the_position_and_the_cash_after(
        self, invested: sqlite3.Connection
    ) -> None:
        proposal = trade(
            invested, ticker="TEST", side="BUY", quantity=2.0, amount_eur=30.0
        )
        assert "Position goes 4 → 6 units" in proposal.summary
        # 90 already spent 40, so cash is 50; 30 more leaves 20.
        assert "€50.00 → €20.00" in proposal.summary
        assert proposal.payload["amount_eur"] == pytest.approx(30.0)

    def test_a_fee_is_charged_on_top_of_the_amount(
        self, invested: sqlite3.Connection
    ) -> None:
        proposal = trade(
            invested,
            ticker="TEST",
            side="BUY",
            quantity=2.0,
            amount_eur=30.0,
            fee_eur=0.35,
        )
        assert "plus a €0.35 fee" in proposal.summary
        assert "€50.00 → €19.65" in proposal.summary

    def test_a_sell_returns_cash_and_reduces_the_position(
        self, invested: sqlite3.Connection
    ) -> None:
        proposal = trade(
            invested, ticker="TEST", side="SELL", quantity=1.0, amount_eur=15.0
        )
        assert "Position goes 4 → 3 units" in proposal.summary
        assert "€50.00 → €65.00" in proposal.summary

    def test_a_lowercase_ticker_is_accepted(self, invested: sqlite3.Connection) -> None:
        assert (
            trade(
                invested, ticker="test", side="buy", quantity=1.0, amount_eur=10.0
            ).payload["ticker"]
            == "TEST"
        )


class TestTradeFromAPrice:
    """ "I bought 2 at 470" — a price per share, in the security's currency."""

    def test_a_native_price_is_converted_and_the_rate_shown(
        self, invested: sqlite3.Connection
    ) -> None:
        # A trade recorded at the wrong FX is a small error that never corrects
        # itself, so which rate was used has to be visible before agreeing.
        proposal = trade(
            invested, ticker="TEST", side="BUY", quantity=2.0, price_native=10.0
        )
        assert proposal.payload["amount_eur"] == pytest.approx(18.0)
        assert "10.0000 USD per unit at 0.9000 USD/EUR" in proposal.summary
        assert proposal.payload["fx_rate"] == pytest.approx(0.9)

    def test_the_native_price_and_rate_are_stored_on_the_trade(
        self, invested: sqlite3.Connection
    ) -> None:
        proposal_id = save(
            invested,
            trade(invested, ticker="TEST", side="BUY", quantity=2.0, price_native=10.0),
        )
        confirm(invested, proposal_id)
        row = invested.execute(
            "SELECT price_native, fx_rate FROM trades ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["price_native"] == pytest.approx(10.0)
        assert row["fx_rate"] == pytest.approx(0.9)

    def test_no_stored_rate_says_to_sync_or_give_euros(
        self, funded: sqlite3.Connection, security_id: int
    ) -> None:
        from models import Trade
        from store import insert_trade

        insert_trade(
            funded,
            Trade(
                security_id=security_id,
                trade_date="2026-01-02",
                side="BUY",
                quantity=1.0,
                amount_eur=10.0,
            ),
        )
        with pytest.raises(ProposalError, match="main.py sync"):
            trade(funded, ticker="TEST", side="BUY", quantity=1.0, price_native=10.0)

    def test_both_an_amount_and_a_price_are_refused(
        self, invested: sqlite3.Connection
    ) -> None:
        with pytest.raises(ProposalError, match="not both and not neither"):
            trade(
                invested,
                ticker="TEST",
                side="BUY",
                quantity=1.0,
                amount_eur=10.0,
                price_native=10.0,
            )

    def test_neither_is_refused(self, invested: sqlite3.Connection) -> None:
        with pytest.raises(ProposalError, match="not both and not neither"):
            trade(invested, ticker="TEST", side="BUY", quantity=1.0)


class TestTradeValidation:
    """The ways a trade proposal must refuse."""

    def test_an_unheld_security_is_refused_and_says_why(
        self, invested: sqlite3.Connection
    ) -> None:
        # A new security needs a currency and feed symbol set deliberately, or
        # it can never be priced.
        with pytest.raises(ProposalError, match="never be priced"):
            trade(invested, ticker="NVDA", side="BUY", quantity=1.0, amount_eur=10.0)

    def test_the_refusal_lists_what_is_held(self, invested: sqlite3.Connection) -> None:
        with pytest.raises(ProposalError, match="TEST"):
            trade(invested, ticker="NVDA", side="BUY", quantity=1.0, amount_eur=10.0)

    def test_selling_more_than_is_held_is_refused(
        self, invested: sqlite3.Connection
    ) -> None:
        # positions() silently caps an over-sell; a proposal must not rely on
        # that, because the cap hides a quantity nobody checked.
        with pytest.raises(ProposalError, match="only 4 are held"):
            trade(invested, ticker="TEST", side="SELL", quantity=5.0, amount_eur=60.0)

    def test_selling_exactly_the_whole_position_is_allowed(
        self, invested: sqlite3.Connection
    ) -> None:
        proposal = trade(
            invested, ticker="TEST", side="SELL", quantity=4.0, amount_eur=60.0
        )
        assert "4 → 0 units" in proposal.summary

    @pytest.mark.parametrize("side", ["HOLD", "", "buy more"])
    def test_a_side_that_is_not_a_direction_is_refused(
        self, invested: sqlite3.Connection, side: str
    ) -> None:
        with pytest.raises(ProposalError, match="BUY or a SELL"):
            trade(invested, ticker="TEST", side=side, quantity=1.0, amount_eur=10.0)

    @pytest.mark.parametrize("quantity", [0, -2, "some", float("nan")])
    def test_an_unusable_quantity_is_refused(
        self, invested: sqlite3.Connection, quantity: object
    ) -> None:
        with pytest.raises(ProposalError):
            trade(
                invested,
                ticker="TEST",
                side="BUY",
                quantity=quantity,  # type: ignore[arg-type]
                amount_eur=10.0,
            )

    def test_a_future_date_is_refused(self, invested: sqlite3.Connection) -> None:
        ahead = (date.today() + timedelta(days=2)).isoformat()
        with pytest.raises(ProposalError, match="in the future"):
            trade(
                invested,
                ticker="TEST",
                side="BUY",
                quantity=1.0,
                amount_eur=10.0,
                day=ahead,
            )

    def test_a_buy_beyond_the_cash_is_allowed_but_flagged(
        self, invested: sqlite3.Connection
    ) -> None:
        # The broker executed it, so the ledger is what is wrong — usually a
        # deposit nobody recorded. Refusing would block a real trade.
        proposal = trade(
            invested, ticker="TEST", side="BUY", quantity=1.0, amount_eur=500.0
        )
        assert "leaves cash negative" in proposal.summary
        assert "a deposit is missing" in proposal.summary


class TestConfirmingATrade:
    """What a tap does to the book."""

    def test_a_buy_moves_the_position_and_the_cash(
        self, invested: sqlite3.Connection
    ) -> None:
        from portfolio import positions

        proposal_id = save(
            invested,
            trade(invested, ticker="TEST", side="BUY", quantity=2.0, amount_eur=30.0),
        )
        message = confirm(invested, proposal_id)
        (position,) = positions(invested, account_id=1)
        assert position.quantity == pytest.approx(6.0)
        assert cash_eur(invested, account_id=1) == pytest.approx(20.0)
        assert "Now holding 6 units" in message

    def test_the_fee_lands_in_the_cost_basis(
        self, invested: sqlite3.Connection
    ) -> None:
        from portfolio import positions

        confirm(
            invested,
            save(
                invested,
                trade(
                    invested,
                    ticker="TEST",
                    side="BUY",
                    quantity=2.0,
                    amount_eur=30.0,
                    fee_eur=0.35,
                ),
            ),
        )
        (position,) = positions(invested, account_id=1)
        assert position.cost_basis_eur == pytest.approx(70.35)

    def test_selling_everything_closes_the_position(
        self, invested: sqlite3.Connection
    ) -> None:
        from portfolio import positions

        confirm(
            invested,
            save(
                invested,
                trade(
                    invested, ticker="TEST", side="SELL", quantity=4.0, amount_eur=60.0
                ),
            ),
        )
        assert positions(invested, account_id=1) == []
        assert cash_eur(invested, account_id=1) == pytest.approx(110.0)

    def test_the_trade_is_marked_as_coming_from_chat(
        self, invested: sqlite3.Connection
    ) -> None:
        # So the eval can check every one of them had a confirmation behind it.
        confirm(
            invested,
            save(
                invested,
                trade(
                    invested, ticker="TEST", side="BUY", quantity=1.0, amount_eur=10.0
                ),
            ),
        )
        note = invested.execute(
            "SELECT note FROM trades ORDER BY id DESC LIMIT 1"
        ).fetchone()["note"]
        assert "via chat" in note

    def test_confirming_twice_records_one_trade(
        self, invested: sqlite3.Connection
    ) -> None:
        # Telegram delivers a double tap as two callbacks, and a duplicated
        # trade is wrong in every figure derived from it afterwards.
        from portfolio import positions

        proposal_id = save(
            invested,
            trade(invested, ticker="TEST", side="BUY", quantity=1.0, amount_eur=10.0),
        )
        confirm(invested, proposal_id)
        confirm(invested, proposal_id)
        (position,) = positions(invested, account_id=1)
        assert position.quantity == pytest.approx(5.0)

    def test_cancelling_records_nothing(self, invested: sqlite3.Connection) -> None:
        proposal_id = save(
            invested,
            trade(invested, ticker="TEST", side="BUY", quantity=1.0, amount_eur=10.0),
        )
        cancel(invested, proposal_id)
        assert invested.execute("SELECT COUNT(*) n FROM trades").fetchone()["n"] == 1


class TestContextNote:
    """One line, standing on its own."""

    def test_the_line_is_dated_and_shown_verbatim(self) -> None:
        proposal = context_note(file="log", text="bought more BRK.B, float argument")
        today = date.today().isoformat()
        assert (
            proposal.payload["line"] == f"- {today} bought more BRK.B, float argument"
        )
        assert proposal.payload["line"] in proposal.summary

    def test_newlines_are_collapsed_to_one_line(self) -> None:
        # A multi-line entry breaks the bullet list it is appended to.
        proposal = context_note(file="log", text="first thought\n\nsecond thought")
        assert "\n" not in proposal.payload["line"]

    @pytest.mark.parametrize("name", ["watchlist", "WATCHLIST", "watchlist.md"])
    def test_the_file_name_is_forgiving(self, name: str) -> None:
        assert context_note(file=name, text="Ferrari").payload["file"] == "watchlist"

    @pytest.mark.parametrize("name", ["strategy", "investor", "history", "portfolio"])
    def test_only_the_owner_s_notes_are_appendable(self, name: str) -> None:
        # strategy.md constrains every recommendation, so changing it is an edit
        # to be read in full, not a line appended from a chat message.
        with pytest.raises(ProposalError, match="log"):
            context_note(file=name, text="something")

    def test_an_empty_note_is_refused(self) -> None:
        with pytest.raises(ProposalError, match="nothing to record"):
            context_note(file="log", text="   ")

    def test_an_overlong_note_says_why_one_line(self) -> None:
        from config import PROPOSAL_NOTE_MAX_CHARS

        with pytest.raises(ProposalError, match="stands on its own"):
            context_note(file="log", text="x" * (PROPOSAL_NOTE_MAX_CHARS + 1))


class TestConfirming:
    """What a tap does."""

    def test_confirming_a_cash_flow_writes_it_and_reports_the_balance(
        self, funded: sqlite3.Connection
    ) -> None:
        proposal_id = save(
            funded, cash_flow(funded, kind="CONTRIBUTION", amount_eur=160.0)
        )
        message = confirm(funded, proposal_id)
        assert cash_eur(funded, account_id=1) == pytest.approx(250.0)
        assert "€250.00" in message
        assert load(funded, proposal_id)["resolution"] == "confirmed"

    def test_the_balance_reported_is_read_back_not_predicted(
        self, funded: sqlite3.Connection
    ) -> None:
        # Something else may have landed between proposing and confirming, and
        # the owner should be told what is true rather than what was expected.
        proposal_id = save(
            funded, cash_flow(funded, kind="CONTRIBUTION", amount_eur=10.0)
        )
        insert_cash_flow(
            funded,
            CashFlow(flow_date="2026-01-02", kind="DIVIDEND", amount_eur=5.0),
        )
        assert "€105.00" in confirm(funded, proposal_id)

    def test_a_confirmed_note_reaches_the_file(
        self, funded: sqlite3.Connection, tmp_path: Path
    ) -> None:
        proposal_id = save(
            funded, context_note(file="log", text="float argument holds")
        )
        confirm(funded, proposal_id, context_dir=tmp_path)
        assert "float argument holds" in (tmp_path / "log.md").read_text()

    def test_a_note_is_appended_not_overwritten(
        self, funded: sqlite3.Connection, tmp_path: Path
    ) -> None:
        (tmp_path / "log.md").write_text("# Log\n\n- 2026-01-01 earlier thought\n")
        confirm(
            funded,
            save(funded, context_note(file="log", text="later thought")),
            context_dir=tmp_path,
        )
        written = (tmp_path / "log.md").read_text()
        assert "earlier thought" in written
        assert written.strip().endswith("later thought")

    def test_a_file_without_a_trailing_newline_does_not_join_two_lines(
        self, funded: sqlite3.Connection, tmp_path: Path
    ) -> None:
        (tmp_path / "log.md").write_text("- 2026-01-01 earlier")
        confirm(
            funded,
            save(funded, context_note(file="log", text="later")),
            context_dir=tmp_path,
        )
        lines = (tmp_path / "log.md").read_text().strip().splitlines()
        assert len(lines) == 2

    def test_no_temporary_file_is_left_behind(
        self, funded: sqlite3.Connection, tmp_path: Path
    ) -> None:
        confirm(
            funded,
            save(funded, context_note(file="log", text="a thought")),
            context_dir=tmp_path,
        )
        assert [path.name for path in tmp_path.iterdir()] == ["log.md"]

    def test_confirming_twice_writes_once(self, funded: sqlite3.Connection) -> None:
        # Telegram delivers a double tap as two callbacks.
        proposal_id = save(
            funded, cash_flow(funded, kind="CONTRIBUTION", amount_eur=10.0)
        )
        confirm(funded, proposal_id)
        assert "Already confirmed" in confirm(funded, proposal_id)
        assert cash_eur(funded, account_id=1) == pytest.approx(100.0)

    def test_cancelling_writes_nothing(self, funded: sqlite3.Connection) -> None:
        proposal_id = save(
            funded, cash_flow(funded, kind="CONTRIBUTION", amount_eur=10.0)
        )
        assert "Nothing was recorded" in cancel(funded, proposal_id)
        assert cash_eur(funded, account_id=1) == pytest.approx(90.0)
        assert load(funded, proposal_id)["resolution"] == "cancelled"

    def test_a_cancelled_proposal_cannot_then_be_confirmed(
        self, funded: sqlite3.Connection
    ) -> None:
        proposal_id = save(
            funded, cash_flow(funded, kind="CONTRIBUTION", amount_eur=10.0)
        )
        cancel(funded, proposal_id)
        assert "Already cancelled" in confirm(funded, proposal_id)
        assert cash_eur(funded, account_id=1) == pytest.approx(90.0)

    def test_an_unknown_id_says_to_ask_again(self, funded: sqlite3.Connection) -> None:
        with pytest.raises(ProposalError, match="Ask again"):
            confirm(funded, 9999)

    def test_what_is_applied_is_the_stored_payload(
        self, funded: sqlite3.Connection
    ) -> None:
        # The sentence the owner agreed to and the write that follows are the
        # same object, so tampering with the row changes both or neither.
        proposal_id = save(
            funded, cash_flow(funded, kind="CONTRIBUTION", amount_eur=10.0)
        )
        stored = json.loads(load(funded, proposal_id)["payload_json"])
        assert stored["amount_eur"] == pytest.approx(10.0)
        confirm(funded, proposal_id)
        assert cash_eur(funded, account_id=1) == pytest.approx(100.0)


class TestExpiry:
    """A proposal is arithmetic against a balance that moves."""

    def _age(self, conn: sqlite3.Connection, proposal_id: int) -> None:
        past = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
        with conn:
            conn.execute(
                "UPDATE pending_write SET expires_at = ? WHERE id = ?",
                (past, proposal_id),
            )

    def test_confirming_an_expired_proposal_is_refused(
        self, funded: sqlite3.Connection
    ) -> None:
        proposal_id = save(
            funded, cash_flow(funded, kind="CONTRIBUTION", amount_eur=10.0)
        )
        self._age(funded, proposal_id)
        with pytest.raises(ProposalError, match="expired"):
            confirm(funded, proposal_id)
        assert cash_eur(funded, account_id=1) == pytest.approx(90.0)
        assert load(funded, proposal_id)["resolution"] == "expired"

    def test_the_sweep_closes_only_unanswered_stale_proposals(
        self, funded: sqlite3.Connection
    ) -> None:
        stale = save(funded, cash_flow(funded, kind="CONTRIBUTION", amount_eur=10.0))
        fresh = save(funded, cash_flow(funded, kind="DIVIDEND", amount_eur=2.0))
        answered = save(funded, cash_flow(funded, kind="FEE", amount_eur=1.0))
        cancel(funded, answered)
        self._age(funded, stale)
        self._age(funded, answered)
        assert expire_stale(funded) == 1
        assert load(funded, stale)["resolution"] == "expired"
        assert load(funded, fresh)["resolved_at"] is None
        assert load(funded, answered)["resolution"] == "cancelled"


class TestSecondAccount:
    """Cash is read for account 1, which is the only one that exists."""

    def test_another_account_s_cash_does_not_move_the_balance(
        self, funded: sqlite3.Connection
    ) -> None:
        second = ensure_account(
            funded,
            Account(name="other", broker="Other", currency="EUR", sync_mode="manual"),
        )
        insert_cash_flow(
            funded,
            CashFlow(
                account_id=second,
                flow_date="2026-01-01",
                kind="CONTRIBUTION",
                amount_eur=1000.0,
            ),
        )
        assert (
            "€90.00 → €250.00"
            in cash_flow(funded, kind="CONTRIBUTION", new_balance_eur=250.0).summary
        )
