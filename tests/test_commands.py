"""Tests for CLI-layer helpers that shape what the user sees."""

from __future__ import annotations

from datetime import date

import pytest

from commands import _eur, _pct, _price_age_days


class TestPriceAge:
    """Staleness decides whether a weekly review is reviewing this week."""

    TODAY = date(2026, 9, 6)

    def test_same_day_is_zero(self) -> None:
        assert _price_age_days("2026-09-06", today=self.TODAY) == 0

    def test_counts_calendar_days(self) -> None:
        # Friday close read on Monday is three days old and entirely normal.
        assert _price_age_days("2026-09-03", today=self.TODAY) == 3

    def test_old_price_reports_its_age(self) -> None:
        assert _price_age_days("2026-07-15", today=self.TODAY) == 53

    def test_absent_date_is_unknown_not_zero(self) -> None:
        # None must not read as "priced today", which would hide a missing feed.
        assert _price_age_days(None, today=self.TODAY) is None

    def test_empty_string_is_unknown(self) -> None:
        assert _price_age_days("", today=self.TODAY) is None

    def test_unparseable_date_is_unknown(self) -> None:
        assert _price_age_days("last tuesday", today=self.TODAY) is None

    def test_future_date_is_negative_not_stale(self) -> None:
        assert _price_age_days("2026-09-08", today=self.TODAY) == -2


class TestFormatting:
    """Unknown values must render as unknown, never as zero."""

    def test_none_renders_as_a_dash(self) -> None:
        assert _eur(None) == "—"
        assert _pct(None) == "—"

    def test_zero_renders_as_zero(self) -> None:
        assert _eur(0.0) == "0.00"

    def test_signed_amounts_carry_their_sign(self) -> None:
        assert _eur(12.5, signed=True) == "+12.50"
        assert _eur(-12.5, signed=True) == "-12.50"

    def test_thousands_are_grouped(self) -> None:
        assert _eur(1376.98) == "1,376.98"

    @pytest.mark.parametrize(
        ("value", "expected"), [(3.456, "+3.46%"), (-0.7, "-0.70%")]
    )
    def test_signed_percentages(self, value: float, expected: str) -> None:
        assert _pct(value, signed=True) == expected
