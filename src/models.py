"""Domain types for the portfolio ledger.

Plain dataclasses, deliberately free of persistence concerns: ``store`` maps
them to and from SQLite, ``portfolio`` derives the computed ones from trades.

Public API:
    Account       -- a broker account holding positions
    Security      -- an instrument the portfolio can hold
    Trade         -- one buy or sell
    CashFlow      -- money entering or leaving the account
    Event         -- a known date worth watching
    ConsensusEstimate -- analyst expectations as observed on one date
    Position      -- a derived holding: quantity and cost basis from trades
    Holding       -- a Position valued at a point in time
    ConcentrationRow -- one grouped weight in a concentration report
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Account:
    """A broker account. v1 has exactly one."""

    name: str
    broker: str
    currency: str
    sync_mode: str
    id: int | None = None


@dataclass(frozen=True)
class Security:
    """An instrument the portfolio can hold.

    Attributes:
        ticker: Canonical symbol as the user knows it, e.g. ``BRK.B``.
        name: Human-readable company name.
        currency: Native listing currency, not the currency of the book.
        asset_class: ``equity``, ``fund`` or ``crypto``.
        pricing_mode: ``feed`` if a market-data provider can price it,
            ``manual`` if it must be entered by hand.
        feed_symbol: Provider symbol when it differs from ``ticker``.
        sector: Broad sector label used for concentration reporting.
        themes: Overlapping theme labels; a security may carry several.
    """

    ticker: str
    name: str
    currency: str = "USD"
    asset_class: str = "equity"
    pricing_mode: str = "feed"
    feed_symbol: str | None = None
    sector: str | None = None
    themes: tuple[str, ...] = ()
    id: int | None = None

    @property
    def price_symbol(self) -> str:
        """Symbol to ask a market-data provider for."""
        return self.feed_symbol or self.ticker


@dataclass(frozen=True)
class Trade:
    """One buy or sell.

    ``price_native`` and ``fx_rate`` are None for synthetic opening trades,
    where only a EUR consideration is known.
    """

    security_id: int
    trade_date: str
    side: str
    quantity: float
    amount_eur: float
    account_id: int = 1
    price_native: float | None = None
    fx_rate: float | None = None
    fee_eur: float = 0.0
    is_synthetic: bool = False
    note: str | None = None
    id: int | None = None


@dataclass(frozen=True)
class CashFlow:
    """Money entering or leaving the account, independent of any security."""

    flow_date: str
    kind: str
    amount_eur: float
    account_id: int = 1
    note: str | None = None
    id: int | None = None


@dataclass(frozen=True)
class Position:
    """A holding derived from the trade ledger.

    Attributes:
        security: The instrument held.
        quantity: Net units held.
        cost_basis_eur: EUR paid for the units still held, fees included.
        realised_pnl_eur: EUR gain or loss already crystallised by sells.
    """

    security: Security
    quantity: float
    cost_basis_eur: float
    realised_pnl_eur: float = 0.0

    @property
    def avg_cost_eur(self) -> float | None:
        """Average EUR cost per unit, or None for a closed position."""
        if self.quantity <= 0:
            return None
        return self.cost_basis_eur / self.quantity


@dataclass(frozen=True)
class Holding:
    """A position valued at a point in time.

    ``value_eur`` is None when nothing can price the security — a manually
    priced instrument with no entered value, or a feed that returned nothing.
    Callers must render that as unknown rather than as zero.
    """

    position: Position
    value_eur: float | None
    price_date: str | None = None
    price_source: str | None = None

    @property
    def unrealised_pnl_eur(self) -> float | None:
        """EUR gain or loss on the units still held."""
        if self.value_eur is None:
            return None
        return self.value_eur - self.position.cost_basis_eur

    @property
    def unrealised_return_pct(self) -> float | None:
        """Unrealised gain as a percentage of cost."""
        pnl = self.unrealised_pnl_eur
        if pnl is None or self.position.cost_basis_eur <= 0:
            return None
        return pnl / self.position.cost_basis_eur * 100


@dataclass(frozen=True)
class ConcentrationRow:
    """One grouped weight in a concentration report."""

    label: str
    value_eur: float
    weight_pct: float
    members: tuple[str, ...] = field(default_factory=tuple)
    over_limit: bool = False


@dataclass(frozen=True)
class Event:
    """A known date worth watching.

    Attributes:
        event_date: ISO date the event falls on.
        kind: Short slug, e.g. ``earnings``, ``product``, ``lockup_expiry``.
        title: Human-readable description.
        source: ``feed``, ``curated`` or ``research``.
        security_id: The holding it concerns, or None for a macro event.
        confidence: ``confirmed`` or ``estimated``. A date a model inferred is
            not the same as one the company announced, and triage must be able
            to tell them apart.
        note: Optional free text.
    """

    event_date: str
    kind: str
    title: str
    source: str
    security_id: int | None = None
    confidence: str = "confirmed"
    note: str | None = None
    id: int | None = None


@dataclass(frozen=True)
class ConsensusEstimate:
    """Analyst expectations for a security, as they stood on one date.

    Keyed by when it was *observed*, not by the period it forecasts: the point
    is the series, because a revision is only visible by comparing today's
    number with the one recorded last week.
    """

    security_id: int
    observed_date: str
    source: str
    period_end: str | None = None
    eps_avg: float | None = None
    eps_low: float | None = None
    eps_high: float | None = None
    revenue_avg: float | None = None
    revenue_low: float | None = None
    revenue_high: float | None = None
