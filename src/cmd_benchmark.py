"""Compare the portfolio against the same money left in a tracker."""

from __future__ import annotations

import argparse
import logging

from profiles import resolve_cli_profile
from store import open_existing_db

logger = logging.getLogger(__name__)


def cmd_benchmark(args: argparse.Namespace) -> None:
    """Sync benchmark history, or show the comparison.

    Raises:
        ValueError: If the benchmark has no stored history.
        ProfileConfigError: If the profile or its database is missing.
    """
    from rich.console import Console
    from rich.table import Table

    from benchmark import compare, sync_benchmark

    console = Console()
    _, db_path = resolve_cli_profile(args.profile, db=args.db)
    conn = open_existing_db(db_path)

    if args.benchmark_cmd == "sync":
        stored = sync_benchmark(conn)
        console.print(f"[green]Stored[/green] {stored} daily close(s).")
        return

    result = compare(conn)

    table = Table(title="Portfolio against doing nothing clever")
    table.add_column("", style="bold")
    table.add_column("Value", justify="right")
    table.add_column("Return", justify="right")
    table.add_row("Money put in", f"€{result.invested_eur:,.2f}", "")
    table.add_row(
        "This portfolio",
        f"€{result.portfolio_eur:,.2f}",
        f"{result.portfolio_return_pct:+.2f}%"
        if result.portfolio_return_pct is not None
        else "—",
    )
    table.add_row(
        f"{result.ticker}",
        f"€{result.benchmark_eur:,.2f}",
        f"{result.benchmark_return_pct:+.2f}%"
        if result.benchmark_return_pct is not None
        else "—",
    )
    table.add_section()
    style = "green" if result.difference_eur >= 0 else "red"
    table.add_row(
        "Difference",
        f"[{style}]€{result.difference_eur:+,.2f}[/{style}]",
        "",
        style="bold",
    )
    console.print(table)

    console.print(
        f"[yellow]{result.verdict}[/yellow]"
        if not result.is_meaningful
        else result.verdict
    )
    console.print(
        f"[dim]{result.name}, priced {result.priced_on}. Money-weighted: each "
        f"euro buys units on the day it arrived, so contributions are compared "
        f"on the same timing as yours.[/dim]"
    )
    if result.unpriced_flows:
        console.print(
            f"[yellow]{len(result.unpriced_flows)} cash flow(s) had no benchmark "
            f"price and were left out: {', '.join(result.unpriced_flows)}.[/yellow]"
        )
