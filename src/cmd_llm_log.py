"""Inspect stored model calls: what was sent, what came back, what it cost."""

from __future__ import annotations

import argparse
import json
import logging

from profiles import resolve_cli_profile
from store import load_llm_calls, open_existing_db

logger = logging.getLogger(__name__)

_PREVIEW_CHARS = 400


def _preview(text: str | None, limit: int = _PREVIEW_CHARS) -> str:
    """Return a single-line excerpt of *text* for a table cell."""
    if not text:
        return "—"
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else f"{flat[:limit]}…"


def cmd_llm_log(args: argparse.Namespace) -> None:
    """List recorded model calls, or show one in full.

    Raises:
        ProfileConfigError: If the profile or its database is missing.
    """
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console()
    _, db_path = resolve_cli_profile(args.profile, db=args.db)
    conn = open_existing_db(db_path)

    rows = load_llm_calls(
        conn,
        call_id=args.id,
        trace_id=args.trace,
        feature=args.feature,
        errors_only=args.errors,
        limit=args.limit,
    )

    if not rows:
        console.print("No matching model calls recorded.")
        return

    if args.id is not None:
        row = rows[0]
        fell_back = (
            ""
            if row["model"] == row["requested_model"]
            else f" (fell back from {row['requested_model']})"
        )
        console.print(
            Panel(
                f"call {row['id']} · {row['feature']} · {row['model']}{fell_back}",
                style="bold",
            )
        )

        meta = Table.grid(padding=(0, 2))
        meta.add_column(style="cyan")
        meta.add_column()
        meta.add_row("when", row["created_at"])
        meta.add_row("trace", str(row["trace_id"]) if row["trace_id"] else "—")
        meta.add_row("attempt", str(row["attempt"]))
        meta.add_row("prompt version", row["prompt_version"] or "—")
        meta.add_row("finish", row["finish_reason"] or "—")
        meta.add_row(
            "tokens",
            f"in {row['input_tokens'] or 0:,} · out {row['output_tokens'] or 0:,} "
            f"· budget {row['max_tokens'] or 0:,}",
        )
        meta.add_row(
            "cost", f"${row['cost_usd']:.6f}" if row["cost_usd"] is not None else "—"
        )
        meta.add_row("latency", f"{row['latency_s']:.2f}s" if row["latency_s"] else "—")
        console.print(meta)

        try:
            messages = json.loads(row["messages_json"])
        except json.JSONDecodeError:
            messages = []
        for message in messages:
            console.print(
                Panel(
                    message.get("content", ""),
                    title=message.get("role", "?"),
                    border_style="blue",
                )
            )

        if row["error"]:
            console.print(Panel(row["error"], title="error", border_style="red"))
        if row["reasoning_text"]:
            console.print(
                Panel(
                    row["reasoning_text"],
                    title="reasoning (not an explanation — what the model emitted)",
                    border_style="yellow",
                )
            )
        if row["response_text"]:
            console.print(
                Panel(row["response_text"], title="response", border_style="green")
            )
        return

    table = Table(title="Model calls")
    table.add_column("ID", justify="right")
    table.add_column("When", style="dim")
    table.add_column("Feature", style="bold")
    table.add_column("Model")
    table.add_column("Tok", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Result", overflow="fold")

    for row in rows:
        result = (
            f"[red]{_preview(row['error'], 80)}[/red]"
            if row["error"]
            else _preview(row["response_text"], 80)
        )
        table.add_row(
            str(row["id"]),
            (row["created_at"] or "")[:16].replace("T", " "),
            row["feature"],
            row["model"],
            f"{(row['input_tokens'] or 0) + (row['output_tokens'] or 0):,}",
            f"${row['cost_usd']:.5f}" if row["cost_usd"] is not None else "—",
            result,
        )
    console.print(table)
    console.print(
        "Full trace of one call: [cyan]main.py llm-log --id N[/cyan]; "
        "a whole operation: [cyan]main.py llm-log --trace N[/cyan]."
    )
