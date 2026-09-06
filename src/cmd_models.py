"""Inspect and change which model each stage of the pipeline calls."""

from __future__ import annotations

import argparse
import logging

from config import FAST_MODEL, FLASH_MODEL, PRO_MODEL
from model_prefs import (
    FEATURE_PURPOSE,
    FEATURES,
    clear_route,
    resolve_route,
)
from profiles import resolve_cli_profile
from store import open_existing_db
from store_research import llm_cost_summary

logger = logging.getLogger(__name__)


def _tier_style(tier: str) -> str:
    """Return a rich colour reflecting how expensive a tier is."""
    return {"flash": "green", "fast": "blue", "pro": "yellow"}.get(tier, "cyan")


def cmd_models(args: argparse.Namespace) -> None:
    """Show or change per-feature model routing.

    Raises:
        ValueError: If a feature or model is unknown.
        ProfileConfigError: If the profile cannot be resolved.
    """
    from rich.console import Console
    from rich.table import Table

    console = Console()

    if args.models_cmd == "set":
        route = resolve_route(args.feature)
        from model_prefs import set_route

        updated = set_route(
            args.feature, model=args.model, temperature=args.temperature
        )
        console.print(
            f"[green]{args.feature}[/green]: {route.model} → "
            f"[bold]{updated.model}[/bold] ({updated.tier})"
        )
        if updated.tier == "pro":
            console.print(
                "[yellow]The pro tier costs roughly four times the flash tier. "
                "Check 'main.py models cost' after a run to see what it "
                "actually changed.[/yellow]"
            )
        return

    if args.models_cmd == "reset":
        clear_route(args.feature)
        target = "every feature" if args.feature == "all" else args.feature
        console.print(f"[green]Reset[/green] {target} to the built-in default.")
        return

    if args.models_cmd == "cost":
        profile, db_path = resolve_cli_profile(args.profile, db=args.db)
        conn = open_existing_db(db_path)
        rows = llm_cost_summary(conn, since=args.since)
        if not rows:
            console.print(
                "No model calls recorded yet."
                if not args.since
                else f"No model calls recorded since {args.since}."
            )
            return

        table = Table(title="Model spend by feature")
        table.add_column("Feature", style="bold")
        table.add_column("Calls", justify="right")
        table.add_column("Failed", justify="right")
        table.add_column("Input tok", justify="right")
        table.add_column("Output tok", justify="right")
        table.add_column("Cost USD", justify="right")

        total = 0.0
        for row in rows:
            total += row["cost_usd"] or 0.0
            failures = row["failures"] or 0
            table.add_row(
                row["feature"],
                f"{row['calls']:,}",
                f"[red]{failures}[/red]" if failures else "0",
                f"{row['input_tokens']:,}",
                f"{row['output_tokens']:,}",
                f"{row['cost_usd']:.4f}",
            )
        table.add_section()
        table.add_row("TOTAL", "", "", "", "", f"{total:.4f}", style="bold")
        console.print(table)
        console.print(
            f"[dim]At this rate a year costs about "
            f"${total * 52:.2f} if that was one week's activity.[/dim]"
        )
        return

    table = Table(title="Model routing")
    table.add_column("Feature", style="bold")
    table.add_column("Model")
    table.add_column("Tier")
    table.add_column("Temp", justify="right")
    table.add_column("Source")
    table.add_column("Purpose", style="dim", overflow="fold")

    for feature in FEATURES:
        route = resolve_route(feature)
        tier = route.tier
        table.add_row(
            feature,
            route.model,
            f"[{_tier_style(tier)}]{tier}[/{_tier_style(tier)}]",
            "—" if route.temperature is None else f"{route.temperature:g}",
            route.source
            if route.source == "default"
            else f"[cyan]{route.source}[/cyan]",
            FEATURE_PURPOSE[feature],
        )
    console.print(table)
    console.print(
        f"[dim]flash={FLASH_MODEL} · fast={FAST_MODEL} · pro={PRO_MODEL}\n"
        f"flash and pro reason before answering; fast does not, which is why it "
        f"is the fallback rather than a cheaper substitute.[/dim]"
    )
    console.print(
        "Change one with [cyan]main.py models set FEATURE --model MODEL[/cyan]; "
        "see spend with [cyan]main.py models cost[/cyan]."
    )
