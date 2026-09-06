"""Market data providers behind a common interface.

The provider is deliberately narrow: given symbols, return the most recent
close in the instrument's own currency; given a currency pair, return a rate.
Everything else — which securities to ask about, how to store the answer, what
to do when one is missing — belongs to the caller, so swapping yfinance for a
paid feed touches only this file.

Nothing here writes to the database, and nothing here decides that a missing
quote is an error. A provider that cannot price a symbol simply omits it from
the result; :mod:`cmd_sync` decides what that means.

Public API:
    Quote                -- one instrument's close in its native currency
    FxQuote              -- one currency pair's rate
    CalendarEntry        -- known dates and analyst consensus for one instrument
    MarketDataProvider   -- the interface adapters implement
    YFinanceProvider     -- free, unofficial, the v1 default
    get_provider         -- construct the configured provider by name

Example:
    from market_data import get_provider

    provider = get_provider()
    quotes = provider.fetch_quotes(["AAPL", "BRK-B"])
    rate = provider.fetch_fx("USD", "EUR")
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from config import MARKET_DATA_LOOKBACK, MARKET_DATA_PROVIDER

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Quote:
    """One instrument's most recent close, in the instrument's own currency."""

    symbol: str
    close: float
    currency: str
    as_of: str
    """ISO date of the close, not of the request. A Monday sync returns
    Friday's close, and storing the request date would make every price look
    fresher than it is."""
    source: str


@dataclass(frozen=True)
class FxQuote:
    """One currency pair's rate: how many *quote* units one *base* unit buys."""

    base: str
    quote: str
    rate: float
    as_of: str
    source: str


@dataclass(frozen=True)
class CalendarEntry:
    """Known dates and analyst consensus for one instrument.

    Every field is optional: coverage varies by instrument, and a newly listed
    company may have an earnings date but no dividend history, while an
    unfollowed one may have neither estimate.
    """

    symbol: str
    source: str
    earnings_dates: tuple[str, ...] = ()
    ex_dividend_date: str | None = None
    dividend_date: str | None = None
    eps_avg: float | None = None
    eps_low: float | None = None
    eps_high: float | None = None
    revenue_avg: float | None = None
    revenue_low: float | None = None
    revenue_high: float | None = None

    @property
    def has_estimates(self) -> bool:
        """True when any consensus figure is present."""
        return any(
            value is not None
            for value in (
                self.eps_avg,
                self.eps_low,
                self.eps_high,
                self.revenue_avg,
                self.revenue_low,
                self.revenue_high,
            )
        )


class MarketDataProvider(Protocol):
    """What the rest of the system needs from a market-data source."""

    name: str

    def fetch_quotes(self, symbols: Sequence[str]) -> dict[str, Quote]:
        """Return quotes keyed by symbol, omitting any that could not be priced."""
        ...

    def fetch_fx(self, base: str, quote: str) -> FxQuote | None:
        """Return the rate for one currency pair, or None if unavailable."""
        ...

    def fetch_calendar(self, symbols: Sequence[str]) -> dict[str, CalendarEntry]:
        """Return known dates and consensus, omitting symbols with neither."""
        ...


class ProviderError(RuntimeError):
    """Raised when a provider fails in a way the caller cannot work around."""


def _last_valid(series: Any) -> tuple[float, str] | None:
    """Return the last non-NaN value in a pandas Series with its ISO date.

    Args:
        series: A pandas Series indexed by timestamp.

    Returns:
        ``(value, iso date)``, or None when the series holds no usable value.
    """
    cleaned = series.dropna()
    if cleaned.empty:
        return None
    value = float(cleaned.iloc[-1])
    if value <= 0:
        return None
    return value, cleaned.index[-1].date().isoformat()


class YFinanceProvider:
    """Free, unofficial Yahoo Finance access via the ``yfinance`` package.

    Chosen for v1 because it needs no key and covers prices and FX in one
    dependency. It is scraped rather than licensed, so it breaks occasionally —
    which is the entire reason this class sits behind
    :class:`MarketDataProvider` rather than being called directly.

    Symbols are Yahoo's, not the broker's: a dotted share class is hyphenated
    (``BRK.B`` is ``BRK-B``), which is what ``securities.feed_symbol`` carries.
    """

    name = "yfinance"

    def __init__(self, lookback: str = MARKET_DATA_LOOKBACK) -> None:
        self._lookback = lookback

    def _download(self, symbols: Sequence[str]) -> Any:
        """Fetch a close-price frame for *symbols*.

        Args:
            symbols: Provider symbols to request.

        Returns:
            A pandas DataFrame of closes, one column per symbol.

        Raises:
            ProviderError: If the request itself fails.
        """
        import warnings

        import yfinance

        unique = sorted(set(symbols))
        logger.debug("Fetching %d symbol(s) from Yahoo", len(unique))
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                frame = yfinance.download(
                    unique,
                    period=self._lookback,
                    progress=False,
                    auto_adjust=True,
                    # Yahoo answers an unknown symbol with a 404 rather than an
                    # empty column. Without this the whole batch raises and one
                    # delisted holding takes the other thirteen with it.
                    ignore_tz=True,
                    threads=True,
                )
        except Exception as exc:  # noqa: BLE001 - provider faults are opaque
            raise ProviderError(f"Yahoo request failed: {exc}") from exc

        if frame is None or frame.empty:
            return None
        # A single symbol yields flat columns; several yield a MultiIndex.
        closes = frame["Close"]
        if len(unique) == 1 and closes.ndim == 1:
            closes = closes.to_frame(name=unique[0])
        return closes

    def fetch_quotes(self, symbols: Sequence[str]) -> dict[str, Quote]:
        """Return quotes keyed by symbol, omitting any Yahoo could not price.

        Currency is not fetched per symbol: doing so costs one extra network
        round trip each, and every holding this system can hold is quoted in
        the currency ``securities.currency`` already records. The caller pairs
        the close with that.

        Args:
            symbols: Provider symbols to request.

        Returns:
            Quotes keyed by the symbol requested.
        """
        if not symbols:
            return {}
        closes = self._download(symbols)
        if closes is None:
            return {}

        quotes: dict[str, Quote] = {}
        for symbol in sorted(set(symbols)):
            if symbol not in closes:
                logger.warning("Yahoo returned no column for %s", symbol)
                continue
            found = _last_valid(closes[symbol])
            if found is None:
                logger.warning("Yahoo returned no usable close for %s", symbol)
                continue
            close, as_of = found
            quotes[symbol] = Quote(
                symbol=symbol,
                close=close,
                # Filled in by the caller from securities.currency; Yahoo's own
                # currency field costs a round trip per symbol to read.
                currency="",
                as_of=as_of,
                source=self.name,
            )
        return quotes

    def fetch_fx(self, base: str, quote: str) -> FxQuote | None:
        """Return the rate for one currency pair, or None if unavailable.

        Yahoo publishes both directions, so ``USDEUR=X`` is requested directly
        rather than inverting ``EURUSD=X``. Inverting is where sign and
        direction errors live, and an FX bug silently misprices every holding.

        Args:
            base: Currency being converted from, e.g. ``USD``.
            quote: Currency being converted to, e.g. ``EUR``.

        Returns:
            The rate, or None when Yahoo has no data for the pair.
        """
        if base == quote:
            return FxQuote(base, quote, 1.0, "", "identity")
        symbol = f"{base}{quote}=X"
        closes = self._download([symbol])
        if closes is None or symbol not in closes:
            logger.warning("Yahoo returned no data for %s", symbol)
            return None
        found = _last_valid(closes[symbol])
        if found is None:
            logger.warning("Yahoo returned no usable rate for %s", symbol)
            return None
        rate, as_of = found
        return FxQuote(base=base, quote=quote, rate=rate, as_of=as_of, source=self.name)

    @staticmethod
    def _as_iso(value: Any) -> str | None:
        """Return an ISO date string from whatever shape Yahoo supplies."""
        if value is None:
            return None
        for attribute in ("date", "isoformat"):
            method = getattr(value, attribute, None)
            if callable(method):
                result = method()
                return (
                    result.isoformat() if hasattr(result, "isoformat") else str(result)
                )
        text = str(value).strip()
        return text[:10] if len(text) >= 10 else None

    @staticmethod
    def _as_float(value: Any) -> float | None:
        """Return a float from a calendar field, or None when absent."""
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        # NaN compares unequal to itself; pandas leaves them all over calendars.
        return None if number != number else number

    def fetch_calendar(self, symbols: Sequence[str]) -> dict[str, CalendarEntry]:
        """Return known dates and consensus estimates per symbol.

        Yahoo exposes the calendar per ticker rather than in a batch, so this
        makes one request each. That is acceptable at a weekly cadence and for
        a portfolio of this size; a provider with a batch endpoint should
        override the shape rather than loop.

        A failure for one symbol is logged and skipped. Calendars are
        supplementary — a missing earnings date degrades triage, it does not
        invalidate a valuation — so one bad ticker must not lose the rest.

        Args:
            symbols: Provider symbols to request.

        Returns:
            Entries keyed by symbol, omitting symbols with nothing useful.
        """
        import warnings

        import yfinance

        entries: dict[str, CalendarEntry] = {}
        for symbol in sorted(set(symbols)):
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    calendar = yfinance.Ticker(symbol).calendar
            except Exception as exc:  # noqa: BLE001 - provider faults are opaque
                logger.warning("Calendar lookup failed for %s: %s", symbol, exc)
                continue
            if not isinstance(calendar, dict) or not calendar:
                logger.debug("No calendar for %s", symbol)
                continue

            raw_earnings = calendar.get("Earnings Date") or []
            if not isinstance(raw_earnings, list | tuple):
                raw_earnings = [raw_earnings]
            earnings = tuple(
                iso for iso in (self._as_iso(item) for item in raw_earnings) if iso
            )
            entry = CalendarEntry(
                symbol=symbol,
                source=self.name,
                earnings_dates=earnings,
                ex_dividend_date=self._as_iso(calendar.get("Ex-Dividend Date")),
                dividend_date=self._as_iso(calendar.get("Dividend Date")),
                eps_avg=self._as_float(calendar.get("Earnings Average")),
                eps_low=self._as_float(calendar.get("Earnings Low")),
                eps_high=self._as_float(calendar.get("Earnings High")),
                revenue_avg=self._as_float(calendar.get("Revenue Average")),
                revenue_low=self._as_float(calendar.get("Revenue Low")),
                revenue_high=self._as_float(calendar.get("Revenue High")),
            )
            if entry.earnings_dates or entry.ex_dividend_date or entry.has_estimates:
                entries[symbol] = entry
        return entries


_PROVIDERS: dict[str, type] = {"yfinance": YFinanceProvider}


def get_provider(name: str = MARKET_DATA_PROVIDER) -> MarketDataProvider:
    """Construct the configured market-data provider.

    Args:
        name: Provider name.

    Returns:
        A ready provider.

    Raises:
        ValueError: If no provider is registered under that name.
    """
    try:
        return _PROVIDERS[name]()
    except KeyError as exc:
        known = ", ".join(sorted(_PROVIDERS))
        raise ValueError(
            f"Unknown market-data provider {name!r}. Available: {known}."
        ) from exc
