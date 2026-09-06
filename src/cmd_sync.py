"""Market-data sync and manual price entry.

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
from datetime import date

from config import BASE_CURRENCY
from market_data import MarketDataProvider, ProviderError, get_provider
from models import Security
from portfolio import positions
from profiles import resolve_cli_profile
from store import open_existing_db, save_fx_rate, save_prices

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


def cmd_sync(args: argparse.Namespace) -> None:
    """Fetch current prices and FX rates for the profile's holdings.

    Raises:
        ProfileConfigError: If the profile or its database is missing.
        ProviderError: If the market-data request fails outright.
    """
    from rich.console import Console
    from rich.table import Table

    console = Console()
    _, db_path = resolve_cli_profile(args.profile, db=args.db)
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
