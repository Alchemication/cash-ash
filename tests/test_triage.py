"""Tests for assembling triage inputs and ranking holdings."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

import research as research_module
from llm import LLMResult
from models import Event, Thesis
from research import run_triage, triage_inputs
from seed import load_snapshot, seed_database
from store import save_events, save_prices
from store_research import save_thesis
from tests.test_seed import FIXTURE

TODAY = date(2026, 9, 6)


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    """A database seeded from the synthetic snapshot fixture."""
    seed_database(conn, load_snapshot(FIXTURE))
    return conn


def _payload(*tickers: str, selected: set[str] | None = None, note: str = "") -> str:
    import json

    chosen = selected or set()
    return json.dumps(
        {
            "rankings": [
                {
                    "ticker": ticker,
                    "rank": index + 1,
                    "selected": ticker in chosen,
                    "reason": f"reason for {ticker}",
                    "signals": ["tag"],
                }
                for index, ticker in enumerate(tickers)
            ],
            "portfolio_note": note,
        }
    )


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch):
    """Replace call_llm with a scripted stand-in, capturing the prompt."""

    def install(text: str):
        seen: list[str] = []

        def fake(conn, *, messages, **kwargs):  # type: ignore[no-untyped-def]
            seen.append(messages[-1]["content"])
            return LLMResult(
                text=text, model="fake", requested_model="fake", llm_call_id=1
            )

        import decisions

        monkeypatch.setattr(research_module, "call_llm", fake)
        monkeypatch.setattr(decisions, "call_llm", fake)
        return seen

    return install


class TestTriageInputs:
    """What triage is told, and what it is told is missing."""

    def test_one_entry_per_holding(self, seeded: sqlite3.Connection) -> None:
        entries = triage_inputs(seeded, today=TODAY)
        assert {entry.ticker for entry in entries} == {"AAA", "BBB", "CCC"}

    def test_includes_weight_and_return(self, seeded: sqlite3.Connection) -> None:
        entry = next(e for e in triage_inputs(seeded, today=TODAY) if e.ticker == "AAA")
        assert entry.weight_pct is not None
        assert entry.unrealised_return_pct == pytest.approx(10.0)

    def test_no_thesis_is_reported_as_absent(self, seeded: sqlite3.Connection) -> None:
        entry = triage_inputs(seeded, today=TODAY)[0]
        assert entry.thesis_summary is None
        assert "thesis: none recorded" in entry.render()

    def test_thesis_and_breaking_conditions_are_included(
        self, seeded: sqlite3.Connection
    ) -> None:
        save_thesis(
            seeded,
            Thesis(
                security_id=1,
                summary="I like it",
                source="user",
                conviction="weak",
                what_would_break_it=("they stop making it",),
                open_questions=("what is the price",),
            ),
        )
        entry = next(e for e in triage_inputs(seeded, today=TODAY) if e.ticker == "AAA")
        rendered = entry.render()
        assert "I like it" in rendered
        assert "they stop making it" in rendered
        assert "what is the price" in rendered

    def test_single_price_yields_no_move(self, seeded: sqlite3.Connection) -> None:
        # One observation is not a trend, and implying one would be inventing
        # data that was never seen.
        save_prices(seeded, [(1, "2026-09-04", 50.0, "USD", "fake")])
        entry = next(e for e in triage_inputs(seeded, today=TODAY) if e.ticker == "AAA")
        assert entry.price_move is None

    def test_two_prices_yield_a_move(self, seeded: sqlite3.Connection) -> None:
        save_prices(
            seeded,
            [
                (1, "2026-08-28", 50.0, "USD", "fake"),
                (1, "2026-09-04", 55.0, "USD", "fake"),
            ],
        )
        entry = next(e for e in triage_inputs(seeded, today=TODAY) if e.ticker == "AAA")
        assert entry.price_move is not None
        assert "+10.0%" in entry.price_move

    def test_single_estimate_yields_no_revision(
        self, seeded: sqlite3.Connection
    ) -> None:
        from models import ConsensusEstimate
        from store import save_consensus

        save_consensus(
            seeded,
            [
                ConsensusEstimate(
                    security_id=1,
                    observed_date="2026-09-06",
                    source="fake",
                    eps_avg=2.0,
                )
            ],
        )
        entry = next(e for e in triage_inputs(seeded, today=TODAY) if e.ticker == "AAA")
        assert entry.estimate_change is None

    def test_estimate_revision_is_detected(self, seeded: sqlite3.Connection) -> None:
        from models import ConsensusEstimate
        from store import save_consensus

        save_consensus(
            seeded,
            [
                ConsensusEstimate(
                    security_id=1,
                    observed_date="2026-08-30",
                    source="fake",
                    eps_avg=2.0,
                ),
                ConsensusEstimate(
                    security_id=1,
                    observed_date="2026-09-06",
                    source="fake",
                    eps_avg=1.8,
                ),
            ],
        )
        entry = next(e for e in triage_inputs(seeded, today=TODAY) if e.ticker == "AAA")
        assert entry.estimate_change is not None
        assert "cut" in entry.estimate_change

    def test_upcoming_and_recent_events_are_split(
        self, seeded: sqlite3.Connection
    ) -> None:
        save_events(
            seeded,
            [
                Event("2026-09-10", "earnings", "AAA earnings", "feed", security_id=1),
                Event("2026-09-02", "product", "AAA launch", "curated", security_id=1),
            ],
        )
        entry = next(e for e in triage_inputs(seeded, today=TODAY) if e.ticker == "AAA")
        assert any("earnings" in item for item in entry.upcoming)
        assert any("launch" in item for item in entry.recent)

    def test_estimated_dates_are_marked(self, seeded: sqlite3.Connection) -> None:
        save_events(
            seeded,
            [
                Event(
                    "2026-09-10",
                    "product",
                    "AAA keynote",
                    "curated",
                    security_id=1,
                    confidence="estimated",
                )
            ],
        )
        entry = next(e for e in triage_inputs(seeded, today=TODAY) if e.ticker == "AAA")
        assert any("~" in item for item in entry.upcoming)

    def test_distant_events_are_excluded(self, seeded: sqlite3.Connection) -> None:
        save_events(
            seeded,
            [Event("2027-06-01", "earnings", "far off", "feed", security_id=1)],
        )
        entry = next(e for e in triage_inputs(seeded, today=TODAY) if e.ticker == "AAA")
        assert entry.upcoming == ()

    def test_macro_events_are_not_attached_to_a_holding(
        self, seeded: sqlite3.Connection
    ) -> None:
        save_events(seeded, [Event("2026-09-10", "macro", "Rate decision", "curated")])
        entries = triage_inputs(seeded, today=TODAY)
        assert all(entry.upcoming == () for entry in entries)


class TestRunTriage:
    """Ranking, and refusing to lose a holding along the way."""

    def test_records_every_holding(self, seeded: sqlite3.Connection, fake_llm) -> None:
        fake_llm(_payload("AAA", "BBB", "CCC", selected={"AAA"}))
        run_id, rankings, _ = run_triage(seeded, today=TODAY)
        assert len(rankings) == 3
        rows = seeded.execute(
            "SELECT COUNT(*) AS n FROM triage_result WHERE run_id = ?", (run_id,)
        ).fetchone()
        assert rows["n"] == 3

    def test_skipped_holdings_are_stored_too(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        # "Nothing needed looking at" is a finding, and it is invisible if only
        # the selected holdings are kept.
        fake_llm(_payload("AAA", "BBB", "CCC", selected={"AAA"}))
        run_id, _, _ = run_triage(seeded, today=TODAY)
        row = seeded.execute(
            "SELECT COUNT(*) AS n FROM triage_result WHERE run_id = ? AND selected = 0",
            (run_id,),
        ).fetchone()
        assert row["n"] == 2

    def test_omitted_holdings_are_recorded_as_unranked(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        # A holding silently dropped looks identical to one considered and
        # passed over, so the gap has to be made explicit.
        fake_llm(_payload("AAA"))
        _, rankings, _ = run_triage(seeded, today=TODAY)
        omitted = [r for r in rankings if "omitted" in r["signals"]]
        assert {r["ticker"] for r in omitted} == {"BBB", "CCC"}
        assert all(r["selected"] is False for r in omitted)

    def test_unknown_tickers_are_discarded(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        fake_llm(_payload("AAA", "BBB", "CCC", "GHOST"))
        _, rankings, _ = run_triage(seeded, today=TODAY)
        assert "GHOST" not in {r["ticker"] for r in rankings}
        assert len(rankings) == 3

    def test_repeated_tickers_are_discarded(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        fake_llm(_payload("AAA", "AAA", "BBB", "CCC"))
        _, rankings, _ = run_triage(seeded, today=TODAY)
        assert len([r for r in rankings if r["ticker"] == "AAA"]) == 1

    def test_rankings_are_sorted(self, seeded: sqlite3.Connection, fake_llm) -> None:
        import json

        fake_llm(
            json.dumps(
                {
                    "rankings": [
                        {"ticker": "CCC", "rank": 3, "selected": False, "reason": "c"},
                        {"ticker": "AAA", "rank": 1, "selected": True, "reason": "a"},
                        {"ticker": "BBB", "rank": 2, "selected": False, "reason": "b"},
                    ],
                    "portfolio_note": "",
                }
            )
        )
        _, rankings, _ = run_triage(seeded, today=TODAY)
        assert [r["ticker"] for r in rankings] == ["AAA", "BBB", "CCC"]

    def test_portfolio_note_is_returned(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        fake_llm(_payload("AAA", "BBB", "CCC", note="Concentrated in one theme."))
        _, _, note = run_triage(seeded, today=TODAY)
        assert note == "Concentrated in one theme."

    def test_prompt_warns_when_no_price_history_exists(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        # Absent data and unchanged data look identical in the rendering, and a
        # model that cannot tell them apart reports calm it never observed.
        seen = fake_llm(_payload("AAA", "BBB", "CCC"))
        run_triage(seeded, today=TODAY)
        assert "No price history is stored yet" in seen[0]

    def test_prompt_warns_when_estimates_have_one_observation(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        seen = fake_llm(_payload("AAA", "BBB", "CCC"))
        run_triage(seeded, today=TODAY)
        assert "no revision can be detected yet" in seen[0]

    def test_no_holdings_is_refused(self, conn: sqlite3.Connection, fake_llm) -> None:
        fake_llm(_payload())
        with pytest.raises(ValueError, match="No holdings to triage"):
            run_triage(conn, today=TODAY)

    def test_unusable_output_is_refused(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        import json

        fake_llm(json.dumps({"rankings": [], "portfolio_note": ""}))
        with pytest.raises(ValueError, match="Nothing was considered"):
            run_triage(seeded, today=TODAY)

    def test_all_unknown_tickers_is_refused(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        # Filling every holding in as omitted would report a triage that
        # considered nothing as though it had run.
        fake_llm(_payload("GHOST", "PHANTOM"))
        with pytest.raises(ValueError, match="Nothing was considered"):
            run_triage(seeded, today=TODAY)

    def test_run_is_recorded(self, seeded: sqlite3.Connection, fake_llm) -> None:
        fake_llm(_payload("AAA", "BBB", "CCC"))
        run_id, _, _ = run_triage(seeded, today=TODAY)
        row = seeded.execute(
            "SELECT kind, run_date FROM research_run WHERE id = ?", (run_id,)
        ).fetchone()
        assert row["kind"] == "triage"
        assert row["run_date"] == "2026-09-06"


class TestSellEligibility:
    """The decision stage is told, as a fact, what the rules will allow."""

    def test_absent_unless_asked_for(self, seeded: sqlite3.Connection) -> None:
        # Triage only ranks; computing it there would be work with no reader.
        entry = triage_inputs(seeded, today=TODAY)[0]
        assert entry.sell_permitted is None
        assert "selling:" not in entry.render()

    @staticmethod
    def _dilute(conn: sqlite3.Connection) -> None:
        """Add cash so every holding sits under the weight cap.

        The fixture is three holdings, so each is 25-42% of it and therefore
        oversized — which permits a trim on size alone, independent of any
        thesis. Diluting isolates the thesis rule from the weight rule.
        """
        from models import CashFlow
        from store import insert_cash_flow

        insert_cash_flow(
            conn,
            CashFlow(flow_date="2026-02-01", kind="CONTRIBUTION", amount_eur=3000.0),
        )

    def test_unexamined_thesis_is_not_sellable(
        self, seeded: sqlite3.Connection
    ) -> None:
        self._dilute(seeded)
        entry = triage_inputs(seeded, today=TODAY, include_sell_eligibility=True)[0]
        assert entry.sell_permitted is False
        assert "NOT permitted" in entry.render()

    def test_an_oversized_position_is_sellable_whatever_the_thesis(
        self, seeded: sqlite3.Connection
    ) -> None:
        from store import save_fx_rate

        save_fx_rate(
            seeded,
            rate_date=TODAY.isoformat(),
            base="USD",
            quote="EUR",
            rate=1,
            source="test",
        )
        save_prices(
            seeded,
            [
                (1, TODAY.isoformat(), 55, "USD", "test"),
                (2, TODAY.isoformat(), 90, "USD", "test"),
                (3, TODAY.isoformat(), 37.5, "USD", "test"),
            ],
        )
        # Trimming for size is a portfolio decision, not a view on the company.
        entry = triage_inputs(seeded, today=TODAY, include_sell_eligibility=True)[0]
        assert entry.weight_pct > 20
        assert entry.sell_permitted is True

    def test_broken_thesis_is_sellable(self, seeded: sqlite3.Connection) -> None:
        from models import Thesis
        from store_research import save_thesis

        self._dilute(seeded)
        from store import save_fx_rate

        save_fx_rate(
            seeded,
            rate_date=TODAY.isoformat(),
            base="USD",
            quote="EUR",
            rate=1,
            source="test",
        )
        save_thesis(
            seeded,
            Thesis(
                security_id=1,
                summary="was true, no longer",
                source="research",
                thesis_status="broken",
            ),
        )
        save_prices(
            seeded,
            [
                (1, TODAY.isoformat(), 55, "USD", "test"),
                (2, TODAY.isoformat(), 90, "USD", "test"),
                (3, TODAY.isoformat(), 37.5, "USD", "test"),
            ],
        )
        entries = {
            e.ticker: e
            for e in triage_inputs(seeded, today=TODAY, include_sell_eligibility=True)
        }
        assert entries["AAA"].sell_permitted is True
        assert "selling: permitted" in entries["AAA"].render()

    def test_the_decision_stage_asks_for_it(
        self, seeded: sqlite3.Connection, fake_llm
    ) -> None:
        # The model proposed a refused sale once, and its other recommendation
        # then referred to a sale that never happened.
        from decisions import run_decision

        self._dilute(seeded)
        seen = fake_llm('{"recommendations": [], "summary": "quiet"}')
        run_decision(seeded, today=TODAY)
        assert "selling: NOT permitted" in seen[0]
