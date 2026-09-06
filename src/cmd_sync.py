"""Market-data sync, calendar sync, manual price entry, and event listing.

``sync`` fetches a close for every held security the configured provider can
price, plus a rate for every currency those securities are quoted in. ``price``
enters one by hand, which is the fallback when a feed cannot or will not price
something.

Kept out of ``commands.py`` from the start, following the split zdrowskit
arrived at once that module outgrew a single file.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta

from config import BASE_CURRENCY
from events_file import load_curated_events
from market_data import MarketDataProvider, ProviderError, get_provider
from models import ConsensusEstimate, Event, Security
from portfolio import positions
from profiles import resolve_cli_profile
from store import (
    load_securities,
    open_existing_db,
    replace_feed_events,
    save_consensus,
    save_events,
    save_fx_rate,
    save_prices,
)

logger = logging.getLogger(__name__)


def sync_prices(
    conn,
    *,
    provider: MarketDataProvider,
    account_id: int = 1,
) -> dict[str, list[str]]:
    """Fetch and store prices and FX rates for every held security.

    Only securities with an open position are fetched: pricing something the
    portfolio no longer holds costs a request and changes no figure.

    Args:
        conn: Open database connection.
        provider: Market-data source.
        account_id: Account whose holdings to price.

    Returns:
        Tickers grouped by outcome — ``priced``, ``unpriced``, ``manual`` — and
        currency pairs under ``fx`` and ``fx_failed``.
    """
    held = [position.security for position in positions(conn, account_id=account_id)]
    feed = [security for security in held if security.pricing_mode == "feed"]
    manual = [security for security in held if security.pricing_mode != "feed"]

    by_symbol: dict[str, Security] = {
        security.price_symbol: security for security in feed
    }
    quotes = provider.fetch_quotes(list(by_symbol)) if by_symbol else {}

    rows: list[tuple[int, str, float, str, str]] = []
    priced: list[str] = []
    for symbol, security in by_symbol.items():
        quote = quotes.get(symbol)
        if quote is None:
            continue
        assert security.id is not None
        rows.append(
            (
                security.id,
                quote.as_of,
                quote.close,
                # The provider reports a number, not a currency; the security
                # record is the authority on what that number is denominated
                # in. Trusting the feed here would let a provider quirk
                # silently redenominate a holding.
                security.currency,
                quote.source,
            )
        )
        priced.append(security.ticker)
    save_prices(conn, rows)

    currencies = sorted(
        {security.currency for security in feed if security.currency != BASE_CURRENCY}
    )
    fx_done: list[str] = []
    fx_failed: list[str] = []
    for currency in currencies:
        fx = provider.fetch_fx(currency, BASE_CURRENCY)
        pair = f"{currency}/{BASE_CURRENCY}"
        if fx is None:
            fx_failed.append(pair)
            continue
        save_fx_rate(
            conn,
            rate_date=fx.as_of or date.today().isoformat(),
            base=fx.base,
            quote=fx.quote,
            rate=fx.rate,
            source=fx.source,
        )
        fx_done.append(f"{pair} {fx.rate:.4f}")

    return {
        "priced": sorted(priced),
        "unpriced": sorted(
            security.ticker
            for symbol, security in by_symbol.items()
            if symbol not in quotes
        ),
        "manual": sorted(security.ticker for security in manual),
        "fx": fx_done,
        "fx_failed": fx_failed,
    }


def sync_calendar(
    conn,
    *,
    provider: MarketDataProvider,
    account_id: int = 1,
    today: str | None = None,
) -> dict[str, int]:
    """Fetch known dates and consensus estimates for held securities.

    Future feed-sourced events are replaced rather than added to, because
    companies reschedule and a stale date beside a new one gives triage two
    answers. Past events are never touched.

    Args:
        conn: Open database connection.
        provider: Market-data source.
        account_id: Account whose holdings to fetch for.
        today: Reference date, for tests.

    Returns:
        Counts under ``events``, ``estimates`` and ``securities``.
    """
    now = today or date.today().isoformat()
    held = [position.security for position in positions(conn, account_id=account_id)]
    by_symbol = {
        security.price_symbol: security
        for security in held
        if security.pricing_mode == "feed"
    }
    if not by_symbol:
        return {"events": 0, "estimates": 0, "securities": 0}

    entries = provider.fetch_calendar(list(by_symbol))

    events: list[Event] = []
    estimates: list[ConsensusEstimate] = []
    for symbol, entry in entries.items():
        security = by_symbol[symbol]
        assert security.id is not None

        for kind, dates in (
            ("earnings", entry.earnings_dates),
            (
                "ex_dividend",
                (entry.ex_dividend_date,) if entry.ex_dividend_date else (),
            ),
            ("dividend", (entry.dividend_date,) if entry.dividend_date else ()),
        ):
            if not dates:
                continue
            replace_feed_events(
                conn, security_id=security.id, kind=kind, on_or_after=now
            )
            label = kind.replace("_", " ")
            events.extend(
                Event(
                    event_date=event_date,
                    kind=kind,
                    title=f"{security.ticker} {label}",
                    source="feed",
                    security_id=security.id,
                )
                for event_date in dates
                if event_date >= now
            )

        if entry.has_estimates:
            estimates.append(
                ConsensusEstimate(
                    security_id=security.id,
                    observed_date=now,
                    source=entry.source,
                    period_end=entry.earnings_dates[0]
                    if entry.earnings_dates
                    else None,
                    eps_avg=entry.eps_avg,
                    eps_low=entry.eps_low,
                    eps_high=entry.eps_high,
                    revenue_avg=entry.revenue_avg,
                    revenue_low=entry.revenue_low,
                    revenue_high=entry.revenue_high,
                )
            )

    written = save_events(conn, events)
    save_consensus(conn, estimates)
    return {
        "events": written,
        "estimates": len(estimates),
        "securities": len(entries),
    }


def import_curated_events(conn, *, path) -> tuple[int, list[str]]:
    """Load the profile's curated events file into the database.

    Args:
        conn: Open database connection.
        path: Curated events file.

    Returns:
        ``(rows inserted, warnings)``.
    """
    tickers = {
        ticker: security.id
        for ticker, security in load_securities(conn).items()
        if security.id is not None
    }
    events, warnings = load_curated_events(path, tickers)
    return save_events(conn, events), warnings


def cmd_sync(args: argparse.Namespace) -> None:
    """Fetch current prices and FX rates for the profile's holdings.

    Raises:
        ProfileConfigError: If the profile or its database is missing.
        ProviderError: If the market-data request fails outright.
    """
    from rich.console import Console
    from rich.table import Table

    console = Console()
    profile, db_path = resolve_cli_profile(args.profile, db=args.db)
    conn = open_existing_db(db_path)
    provider = get_provider(args.provider) if args.provider else get_provider()

    console.print(f"Fetching from [cyan]{provider.name}[/cyan]…")
    try:
        result = sync_prices(conn, provider=provider)
    except ProviderError as exc:
        raise ProviderError(
            f"{exc} Prices were left unchanged; 'holdings' will keep using the "
            f"last ones it had and mark them stale."
        ) from exc

    table = Table(title="Sync")
    table.add_column("Outcome", style="bold")
    table.add_column("Count", justify="right")
    table.add_column("Detail", style="dim", overflow="fold")
    table.add_row("Priced", str(len(result["priced"])), ", ".join(result["priced"]))
    if result["unpriced"]:
        table.add_row(
            "[yellow]Not priced[/yellow]",
            str(len(result["unpriced"])),
            ", ".join(result["unpriced"]),
        )
    if result["manual"]:
        table.add_row("Manual", str(len(result["manual"])), ", ".join(result["manual"]))
    table.add_row("FX", str(len(result["fx"])), ", ".join(result["fx"]))
    if result["fx_failed"]:
        table.add_row(
            "[red]FX failed[/red]",
            str(len(result["fx_failed"])),
            ", ".join(result["fx_failed"]),
        )
    console.print(table)

    if not args.prices_only:
        console.print("Fetching known dates and consensus estimates…")
        calendar = sync_calendar(conn, provider=provider)
        console.print(
            f"Calendar: [bold]{calendar['events']}[/bold] new event(s), "
            f"[bold]{calendar['estimates']}[/bold] estimate snapshot(s) across "
            f"{calendar['securities']} securities"
        )
        if profile is not None:
            added, warnings = import_curated_events(conn, path=profile.events_file)
            if added:
                console.print(f"Curated: [bold]{added}[/bold] new event(s)")
            for warning in warnings:
                console.print(f"[yellow]events.toml — {warning}[/yellow]")

    if result["unpriced"] or result["manual"]:
        names = ", ".join(result["unpriced"] + result["manual"])
        console.print(
            f"[yellow]Enter a price by hand for: {names}[/yellow]\n"
            f"  uv run python main.py price TICKER --close AMOUNT"
        )
    console.print("Next: [cyan]uv run python main.py holdings[/cyan]")


def cmd_price(args: argparse.Namespace) -> None:
    """Record one price by hand.

    The fallback for anything a feed will not price — a delisting, a halt, a
    provider outage, or an instrument that was never listed.

    Raises:
        ProfileConfigError: If the profile or its database is missing.
        ValueError: If the ticker is unknown or the price is not positive.
    """
    from rich.console import Console

    from store import load_securities

    console = Console()
    _, db_path = resolve_cli_profile(args.profile, db=args.db)
    conn = open_existing_db(db_path)

    securities = load_securities(conn)
    security = securities.get(args.ticker.upper())
    if security is None:
        known = ", ".join(sorted(securities))
        raise ValueError(f"Unknown ticker {args.ticker!r}. Known: {known}.")
    if args.close <= 0:
        raise ValueError("A price must be positive.")

    assert security.id is not None
    price_date = args.date or date.today().isoformat()
    save_prices(
        conn,
        [
            (
                security.id,
                price_date,
                args.close,
                args.currency or security.currency,
                "manual",
            )
        ],
    )
    console.print(
        f"[green]Recorded[/green] {security.ticker} at "
        f"{args.close:,.2f} {args.currency or security.currency} on {price_date}"
    )


def cmd_events(args: argparse.Namespace) -> None:
    """List known upcoming events for the profile's holdings.

    Raises:
        ProfileConfigError: If the profile or its database is missing.
    """
    from rich.console import Console
    from rich.table import Table

    from store import load_events

    console = Console()
    _, db_path = resolve_cli_profile(args.profile, db=args.db)
    conn = open_existing_db(db_path)

    today = date.today()
    start = today.isoformat() if not args.past else None
    end = (today + timedelta(days=args.days)).isoformat()
    rows = load_events(conn, start=start, end=end)

    if not rows:
        console.print(
            f"No events recorded in the next {args.days} days. Run "
            f"[cyan]uv run python main.py sync[/cyan] to fetch earnings dates, "
            f"and add the ones no feed carries to your events.toml."
        )
        return

    table = Table(title=f"Events — next {args.days} days")
    table.add_column("Date", style="bold")
    table.add_column("In", justify="right")
    table.add_column("Ticker")
    table.add_column("Kind")
    table.add_column("Event", overflow="fold")
    table.add_column("Source", style="dim")

    for row in rows:
        days = (date.fromisoformat(row["event_date"]) - today).days
        when = f"{days}d" if days >= 0 else f"{-days}d ago"
        marker = "" if row["confidence"] == "confirmed" else " [yellow]~[/yellow]"
        table.add_row(
            row["event_date"],
            when,
            row["ticker"] or "[dim]—[/dim]",
            row["kind"].replace("_", " "),
            row["title"] + marker,
            row["source"],
        )
    console.print(table)
    console.print("[dim]~ marks an estimated date. Macro events show no ticker.[/dim]")
