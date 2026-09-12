"""Tests for how portfolio data reads to the model.

The tool tests cover the whole path; these cover the edges a tool cannot easily
reach — a zero total, a return of exactly nothing, a value that is absent rather
than small. Each is a case where the wrong rendering would be read as a number
instead of as a gap.
"""

from __future__ import annotations

import pytest

from chat_render import (
    MAX_CELL_CHARS,
    UNPRICED,
    cell,
    eur,
    holding_row,
    holdings_table,
    one_holding,
    pct,
    quantity,
    rows_table,
    table,
    weight_pct,
)
from models import Holding, Position, Security


def _holding(
    *,
    ticker: str = "TEST",
    value: float | None = 100.0,
    qty: float = 2.0,
    cost: float = 80.0,
    realised: float = 0.0,
) -> Holding:
    return Holding(
        position=Position(
            security=Security(
                ticker=ticker, name=f"{ticker} Inc", currency="EUR", sector="Technology"
            ),
            quantity=qty,
            cost_basis_eur=cost,
            realised_pnl_eur=realised,
        ),
        value_eur=value,
        price_date="2026-09-10" if value is not None else None,
        price_source="feed" if value is not None else None,
    )


class TestNumbers:
    """Formatting a model reads back verbatim, so it must not invent precision."""

    def test_money_is_cents_with_separators(self) -> None:
        assert eur(1195.736) == "€1,195.74"
        assert eur(-30.0) == "€-30.00"

    def test_a_percentage_can_carry_its_sign(self) -> None:
        assert pct(6.93) == "6.9%"
        assert pct(6.93, signed=True) == "+6.9%"
        assert pct(-9.87, signed=True) == "-9.9%"

    @pytest.mark.parametrize(
        ("value", "rendered"),
        [(2.0, "2"), (1.5550, "1.555"), (0.0741, "0.0741"), (0.0, "0")],
    )
    def test_a_quantity_drops_invented_precision(
        self, value: float, rendered: str
    ) -> None:
        # 2.0 shares is two shares; 0.0741 is a real fractional holding.
        assert quantity(value) == rendered


class TestCells:
    """Arbitrary SQL values, rendered without breaking the table around them."""

    def test_null_is_named(self) -> None:
        assert cell(None) == "NULL"

    def test_a_bool_is_a_word(self) -> None:
        assert cell(True) == "true"

    def test_a_float_artefact_is_trimmed(self) -> None:
        assert cell(50.333333333333336) == "50.3333"

    def test_a_pipe_is_escaped_and_a_newline_flattened(self) -> None:
        assert cell("a|b\nc") == "a\\|b c"

    def test_an_oversized_value_says_its_real_length(self) -> None:
        rendered = cell("x" * (MAX_CELL_CHARS + 50))
        assert "truncated" in rendered
        assert str(MAX_CELL_CHARS + 50) in rendered

    def test_bytes_survive_as_text(self) -> None:
        assert cell(b"abc") == "abc"


class TestTables:
    """The shape the model parses."""

    def test_a_table_has_a_header_and_a_rule(self) -> None:
        rendered = table(["a", "b"], [["1", "2"]])
        assert rendered.splitlines() == ["| a | b |", "| --- | --- |", "| 1 | 2 |"]

    def test_no_rows_says_the_query_matched_nothing(self) -> None:
        # Not an empty table: silence reads as a failure.
        assert "matched nothing" in rows_table(["a"], [], 50)

    def test_reaching_the_limit_is_declared(self) -> None:
        assert "there may be more" in rows_table(["a"], [{"a": 1}, {"a": 2}], 2)

    def test_staying_under_the_limit_is_not(self) -> None:
        assert "there may be more" not in rows_table(["a"], [{"a": 1}], 50)

    def test_a_missing_column_renders_as_null(self) -> None:
        assert "| NULL |" in rows_table(["a", "b"], [{"a": 1}], 50)


class TestWeight:
    """A weight is given to the model so it never picks its own denominator."""

    def test_a_weight_is_a_share_of_the_total(self) -> None:
        assert weight_pct(_holding(value=100.0), 400.0) == pytest.approx(25.0)

    def test_an_unpriced_holding_has_no_weight(self) -> None:
        assert weight_pct(_holding(value=None), 400.0) is None

    @pytest.mark.parametrize("total", [0.0, -5.0])
    def test_an_empty_total_yields_no_weight_rather_than_dividing(
        self, total: float
    ) -> None:
        # A zero total happens on an unseeded book; dividing would raise, and a
        # weight of zero would be a lie.
        assert weight_pct(_holding(value=100.0), total) is None


class TestHoldingsTable:
    """The portfolio as the model sees it."""

    def _rendered(self, rows: list[Holding], gaps: list[str] | None = None) -> str:
        return holdings_table(
            rows,
            total=400.0,
            cash=300.0,
            gaps=gaps or [],
            as_of="2026-09-12",
        )

    def test_an_unpriced_holding_is_marked_and_explained(self) -> None:
        rendered = self._rendered(
            [_holding(), _holding(ticker="DARK", value=None)],
            gaps=["DARK: unpriced"],
        )
        assert f"| {UNPRICED} |" in rendered
        assert "UNPRICED: DARK" in rendered
        assert "the total is a floor" in rendered
        assert "Why: DARK: unpriced." in rendered

    def test_a_fully_priced_portfolio_carries_no_warning(self) -> None:
        rendered = self._rendered([_holding()])
        assert "UNPRICED" not in rendered
        assert "floor" not in rendered

    def test_the_priced_count_is_singular_when_it_is_one(self) -> None:
        assert "1 priced holding." in self._rendered([_holding()])

    def test_provenance_travels_with_the_value(self) -> None:
        assert "2026-09-10 (feed)" in self._rendered([_holding()])

    def test_an_unpriced_holding_reports_never_priced(self) -> None:
        assert "| never |" in self._rendered([_holding(value=None)])


class TestOneHolding:
    """A single holding gets prose, since there is no token pressure."""

    def test_it_names_what_a_table_leaves_out(self) -> None:
        rendered = one_holding(_holding(), total=400.0, cash=300.0, gaps=[])
        assert "TEST — TEST Inc" in rendered
        assert "Sector: Technology" in rendered
        assert "quoted in EUR" in rendered
        assert "|" not in rendered

    def test_an_unpriced_holding_is_told_not_to_be_zeroed(self) -> None:
        rendered = one_holding(_holding(value=None), total=400.0, cash=300.0, gaps=[])
        assert "Do not report it as zero" in rendered
        assert "has no weight" in rendered

    def test_realised_profit_is_mentioned_only_when_there_is_some(self) -> None:
        assert "Already realised" not in one_holding(
            _holding(), total=400.0, cash=300.0, gaps=[]
        )
        assert "Already realised" in one_holding(
            _holding(realised=12.5), total=400.0, cash=300.0, gaps=[]
        )

    def test_an_unclassified_sector_is_named_as_such(self) -> None:
        holding = Holding(
            position=Position(
                security=Security(ticker="X", name="X Ltd", currency="EUR"),
                quantity=1.0,
                cost_basis_eur=10.0,
            ),
            value_eur=12.0,
        )
        assert "unclassified" in one_holding(holding, total=100.0, cash=50.0, gaps=[])

    def test_only_this_holding_s_pricing_problems_are_shown(self) -> None:
        rendered = one_holding(
            _holding(),
            total=400.0,
            cash=300.0,
            gaps=["TEST: price stale", "OTHER: unpriced"],
        )
        assert "TEST: price stale" in rendered
        assert "OTHER" not in rendered


class TestHoldingRow:
    """Structured rows, for a chart rather than for reading."""

    def test_an_unpriced_holding_keeps_its_nulls(self) -> None:
        row = holding_row(_holding(value=None), 400.0)
        assert row["value_eur"] is None
        assert row["weight_pct"] is None
        assert row["cost_basis_eur"] == pytest.approx(80.0)

    def test_a_priced_holding_carries_its_weight(self) -> None:
        assert holding_row(_holding(value=100.0), 400.0)["weight_pct"] == pytest.approx(
            25.0
        )
