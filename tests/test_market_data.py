"""Tests for market-data providers and the sync that stores their output.

No test here reaches the network. ``YFinanceProvider`` is exercised only
through its pure parsing helper; the sync is driven by a fake provider, which
is the point of having the abstraction at all.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

import pytest

from cmd_sync import sync_prices
from market_data import (
    FxQuote,
    MarketDataProvider,
    ProviderError,
    Quote,
    YFinanceProvider,
    _last_valid,
    get_provider,
)
from models import Security, Trade
from seed import load_snapshot, seed_database
from store import (
    insert_trade,
    latest_prices,
    save_fx_rate,
    save_prices,
    upsert_security,
)
from tests.test_seed import FIXTURE


class FakeProvider:
    """A provider that returns exactly what a test tells it to."""

    name = "fake"

    def __init__(
        self,
        quotes: dict[str, Quote] | None = None,
        fx: dict[tuple[str, str], FxQuote] | None = None,
    ) -> None:
        self._quotes = quotes or {}
        self._fx = fx or {}
        self.asked_for: list[str] = []

    def fetch_quotes(self, symbols: Sequence[str]) -> dict[str, Quote]:
        self.asked_for = sorted(symbols)
        return {
            symbol: quote for symbol, quote in self._quotes.items() if symbol in symbols
        }

    def fetch_fx(self, base: str, quote: str) -> FxQuote | None:
        return self._fx.get((base, quote))


def _quote(symbol: str, close: float, as_of: str = "2026-09-04") -> Quote:
    return Quote(symbol=symbol, close=close, currency="", as_of=as_of, source="fake")


class TestProviderProtocol:
    """The fake and the real provider must satisfy the same interface."""

    def test_fake_satisfies_the_protocol(self) -> None:
        provider: MarketDataProvider = FakeProvider()
        assert provider.name == "fake"

    def test_yfinance_satisfies_the_protocol(self) -> None:
        provider: MarketDataProvider = YFinanceProvider()
        assert provider.name == "yfinance"

    def test_factory_returns_the_default(self) -> None:
        assert get_provider("yfinance").name == "yfinance"

    def test_factory_rejects_unknown_names(self) -> None:
        with pytest.raises(ValueError, match="Unknown market-data provider"):
            get_provider("bloomberg")


class TestYFinanceFailures:
    """A scraped feed fails in ordinary operation; it must fail legibly."""

    def test_request_failure_becomes_a_provider_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import yfinance

        def boom(*args, **kwargs):
            raise ConnectionError("simulated outage")

        monkeypatch.setattr(yfinance, "download", boom)
        with pytest.raises(ProviderError, match="Yahoo request failed"):
            YFinanceProvider().fetch_quotes(["AAPL"])

    def test_empty_frame_yields_no_quotes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import pandas as pd
        import yfinance

        monkeypatch.setattr(yfinance, "download", lambda *a, **k: pd.DataFrame())
        assert YFinanceProvider().fetch_quotes(["AAPL"]) == {}

    def test_empty_frame_yields_no_fx(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import pandas as pd
        import yfinance

        monkeypatch.setattr(yfinance, "download", lambda *a, **k: pd.DataFrame())
        assert YFinanceProvider().fetch_fx("USD", "EUR") is None

    def test_no_symbols_makes_no_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import yfinance

        def boom(*args, **kwargs):
            raise AssertionError("should not have been called")

        monkeypatch.setattr(yfinance, "download", boom)
        assert YFinanceProvider().fetch_quotes([]) == {}

    def test_identical_currencies_short_circuit(self) -> None:
        # Never worth a network call, and a provider that returned anything but
        # 1.0 here would be wrong.
        fx = YFinanceProvider().fetch_fx("EUR", "EUR")
        assert fx is not None and fx.rate == 1.0


class TestLastValid:
    """Parsing a price series without reaching the network."""

    @staticmethod
    def _series(values: list[float | None]):
        import pandas as pd

        index = pd.to_datetime(
            [f"2026-09-{day:02d}" for day in range(1, len(values) + 1)]
        )
        return pd.Series(values, index=index, dtype="float64")

    def test_returns_last_value_with_its_date(self) -> None:
        found = _last_valid(self._series([10.0, 11.0, 12.0]))
        assert found == (12.0, "2026-09-03")

    def test_skips_trailing_nans(self) -> None:
        found = _last_valid(self._series([10.0, 11.0, None]))
        assert found == (11.0, "2026-09-02")

    def test_all_nan_returns_none(self) -> None:
        assert _last_valid(self._series([None, None])) is None

    def test_empty_returns_none(self) -> None:
        assert _last_valid(self._series([])) is None

    def test_non_positive_close_is_rejected(self) -> None:
        # A zero close is Yahoo malfunctioning, not a free share.
        assert _last_valid(self._series([10.0, 0.0])) is None


class TestPriceStorage:
    """Prices and FX rates round-trip and upsert."""

    def test_prices_round_trip(
        self, conn: sqlite3.Connection, security_id: int
    ) -> None:
        save_prices(conn, [(security_id, "2026-09-04", 100.0, "USD", "fake")])
        row = latest_prices(conn)[security_id]
        assert row["close_native"] == 100.0
        assert row["currency"] == "USD"

    def test_same_date_upserts_rather_than_duplicating(
        self, conn: sqlite3.Connection, security_id: int
    ) -> None:
        save_prices(conn, [(security_id, "2026-09-04", 100.0, "USD", "fake")])
        save_prices(conn, [(security_id, "2026-09-04", 110.0, "USD", "fake")])
        count = conn.execute("SELECT COUNT(*) AS n FROM prices").fetchone()["n"]
        assert count == 1
        assert latest_prices(conn)[security_id]["close_native"] == 110.0

    def test_empty_write_is_a_no_op(self, conn: sqlite3.Connection) -> None:
        assert save_prices(conn, []) == 0

    def test_fx_round_trips(self, conn: sqlite3.Connection) -> None:
        save_fx_rate(
            conn,
            rate_date="2026-09-04",
            base="USD",
            quote="EUR",
            rate=0.8605,
            source="fake",
        )
        row = conn.execute("SELECT rate FROM fx_rates").fetchone()
        assert row["rate"] == pytest.approx(0.8605)

    @pytest.mark.parametrize("rate", [0.0, -1.0])
    def test_non_positive_fx_is_refused(
        self, conn: sqlite3.Connection, rate: float
    ) -> None:
        # A zero rate would value the entire portfolio at nothing, silently.
        with pytest.raises(ValueError, match="non-positive"):
            save_fx_rate(
                conn,
                rate_date="2026-09-04",
                base="USD",
                quote="EUR",
                rate=rate,
                source="fake",
            )


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    """A database seeded from the synthetic snapshot fixture."""
    seed_database(conn, load_snapshot(FIXTURE))
    return conn


class TestSyncPrices:
    """What sync asks for, what it stores, and what it reports."""

    def test_prices_every_held_security(self, seeded: sqlite3.Connection) -> None:
        provider = FakeProvider(
            {
                "AAA": _quote("AAA", 60.0),
                "BBB-X": _quote("BBB-X", 90.0),
                "CCC": _quote("CCC", 40.0),
            },
            {("USD", "EUR"): FxQuote("USD", "EUR", 0.86, "2026-09-04", "fake")},
        )
        result = sync_prices(seeded, provider=provider)
        assert result["priced"] == ["AAA", "BBB", "CCC"]
        assert result["unpriced"] == []

    def test_asks_using_the_feed_symbol(self, seeded: sqlite3.Connection) -> None:
        provider = FakeProvider()
        sync_prices(seeded, provider=provider)
        # BBB carries feed_symbol BBB-X; the broker's ticker must not be sent.
        assert "BBB-X" in provider.asked_for
        assert "BBB" not in provider.asked_for

    def test_missing_quote_is_reported_not_invented(
        self, seeded: sqlite3.Connection
    ) -> None:
        provider = FakeProvider({"AAA": _quote("AAA", 60.0)})
        result = sync_prices(seeded, provider=provider)
        assert result["priced"] == ["AAA"]
        assert result["unpriced"] == ["BBB", "CCC"]

    def test_currency_comes_from_the_security_not_the_feed(
        self, seeded: sqlite3.Connection
    ) -> None:
        # The provider reports a bare number. If the feed were trusted to say
        # what currency it is, a provider quirk could redenominate a holding.
        provider = FakeProvider({"AAA": _quote("AAA", 60.0)})
        sync_prices(seeded, provider=provider)
        row = seeded.execute(
            "SELECT p.currency FROM prices p JOIN securities s ON s.id = p.security_id "
            "WHERE s.ticker = 'AAA'"
        ).fetchone()
        assert row["currency"] == "USD"

    def test_stores_the_close_date_not_todays(self, seeded: sqlite3.Connection) -> None:
        provider = FakeProvider({"AAA": _quote("AAA", 60.0, as_of="2026-08-28")})
        sync_prices(seeded, provider=provider)
        row = seeded.execute("SELECT price_date FROM prices").fetchone()
        assert row["price_date"] == "2026-08-28"

    def test_manual_securities_are_skipped_not_failed(
        self, seeded: sqlite3.Connection
    ) -> None:
        upsert_security(
            seeded,
            Security(
                ticker="AAA",
                name="Alpha Industries",
                currency="USD",
                pricing_mode="manual",
            ),
        )
        provider = FakeProvider({"BBB-X": _quote("BBB-X", 90.0)})
        result = sync_prices(seeded, provider=provider)
        assert result["manual"] == ["AAA"]
        assert "AAA" not in result["unpriced"]
        assert "AAA" not in provider.asked_for

    def test_does_not_price_a_sold_out_position(
        self, seeded: sqlite3.Connection
    ) -> None:
        security_id = seeded.execute(
            "SELECT id FROM securities WHERE ticker = 'AAA'"
        ).fetchone()["id"]
        insert_trade(
            seeded,
            Trade(
                security_id=security_id,
                trade_date="2026-02-01",
                side="SELL",
                quantity=2.0,
                amount_eur=120.0,
            ),
        )
        provider = FakeProvider()
        sync_prices(seeded, provider=provider)
        assert "AAA" not in provider.asked_for

    def test_fx_is_fetched_once_per_currency(self, seeded: sqlite3.Connection) -> None:
        provider = FakeProvider(
            fx={("USD", "EUR"): FxQuote("USD", "EUR", 0.86, "2026-09-04", "fake")}
        )
        result = sync_prices(seeded, provider=provider)
        assert result["fx"] == ["USD/EUR 0.8600"]
        rows = seeded.execute("SELECT COUNT(*) AS n FROM fx_rates").fetchone()
        assert rows["n"] == 1

    def test_failed_fx_is_reported(self, seeded: sqlite3.Connection) -> None:
        result = sync_prices(seeded, provider=FakeProvider())
        assert result["fx"] == []
        assert result["fx_failed"] == ["USD/EUR"]

    def test_running_twice_does_not_duplicate(self, seeded: sqlite3.Connection) -> None:
        provider = FakeProvider(
            {"AAA": _quote("AAA", 60.0)},
            {("USD", "EUR"): FxQuote("USD", "EUR", 0.86, "2026-09-04", "fake")},
        )
        sync_prices(seeded, provider=provider)
        sync_prices(seeded, provider=provider)
        prices = seeded.execute("SELECT COUNT(*) AS n FROM prices").fetchone()["n"]
        rates = seeded.execute("SELECT COUNT(*) AS n FROM fx_rates").fetchone()["n"]
        assert (prices, rates) == (1, 1)


class TestValuationAfterSync:
    """The stored price and rate must actually reach the valuation."""

    def test_holding_is_valued_from_price_times_rate(
        self, seeded: sqlite3.Connection
    ) -> None:
        from portfolio import holdings

        provider = FakeProvider(
            {"AAA": _quote("AAA", 50.0)},
            {("USD", "EUR"): FxQuote("USD", "EUR", 0.80, "2026-09-04", "fake")},
        )
        sync_prices(seeded, provider=provider)
        rows = {row.position.security.ticker: row for row in holdings(seeded)}
        # AAA holds 2 units at USD 50, converted at 0.80.
        assert rows["AAA"].value_eur == pytest.approx(80.0)
        assert rows["AAA"].price_source == "fake"

    def test_unsynced_holding_falls_back_to_the_snapshot(
        self, seeded: sqlite3.Connection
    ) -> None:
        from portfolio import holdings

        provider = FakeProvider(
            {"AAA": _quote("AAA", 50.0)},
            {("USD", "EUR"): FxQuote("USD", "EUR", 0.80, "2026-09-04", "fake")},
        )
        sync_prices(seeded, provider=provider)
        rows = {row.position.security.ticker: row for row in holdings(seeded)}
        # BBB was never priced, so it keeps the snapshot's EUR 90 valuation.
        assert rows["BBB"].value_eur == pytest.approx(90.0)
        assert rows["BBB"].price_source == "testbroker"
