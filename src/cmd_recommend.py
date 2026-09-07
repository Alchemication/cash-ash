"""Produce and review portfolio recommendations."""

from __future__ import annotations

import argparse
import logging
from datetime import date

from profiles import resolve_cli_profile
from store import open_existing_db

logger = logging.getLogger(__name__)

_ACTION_STYLE = {
    "BUY": "green",
    "ADD": "green",
    "TRIM": "yellow",
    "EXIT": "red",
    "REVIEW": "cyan",
    "KEEP_CASH": "blue",
    "HOLD": "dim",
}


def cmd_recommend(args: argparse.Namespace) -> None:
    """Propose actions for the week, with the deterministic rules applied.

    Raises:
        ValueError: If there is nothing to decide on, or output is unusable.
        ProfileConfigError: If the profile or its database is missing.
    """
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    from research import build_guardrail_context, run_decision

    console = Console()
    profile, db_path = resolve_cli_profile(args.profile, db=args.db)
    conn = open_existing_db(db_path)
    context = build_guardrail_context(conn, profile=profile)

    console.print(
        f"Capital available: [bold]EUR {context.available_capital_eur:,.2f}[/bold] "
        f"({context.cash_eur:,.2f} cash + {context.monthly_contribution_eur:,.2f} "
        f"planned)\n"
    )

    run_id, recommendations, summary = run_decision(conn, profile=profile)

    live = [item for item in recommendations if not item["refused"]]
    refused = [item for item in recommendations if item["refused"]]

    if not live:
        console.print(
            "[green]Nothing to do this week.[/green] That is the system working, "
            "not failing."
        )
    else:
        table = Table(title=f"Recommendations — run {run_id}")
        table.add_column("Action", style="bold")
        table.add_column("Ticker")
        table.add_column("Amount", justify="right")
        table.add_column("Urgency")
        table.add_column("Why", overflow="fold")
        for item in live:
            style = _ACTION_STYLE.get(item["action"], "white")
            amount = (
                f"EUR {item['amount_eur']:,.2f}"
                if item["amount_eur"] is not None
                else "—"
            )
            table.add_row(
                f"[{style}]{item['action']}[/{style}]",
                item["ticker"] or "—",
                amount,
                item["urgency"],
                item["rationale"],
            )
        console.print(table)

        for item in live:
            for adjustment in item["adjustments"]:
                console.print(
                    f"[yellow]adjusted[/yellow] {item['action']} "
                    f"{item['ticker'] or ''}: {adjustment}"
                )

    if refused:
        console.print()
        for item in refused:
            console.print(
                f"[red]refused[/red] {item['action']} {item['ticker'] or ''} — "
                f"{item['refusal']}"
            )

    if summary:
        console.print(Panel(summary, title="the week", border_style="cyan"))
    if live:
        console.print(
            "Nothing has been bought or sold. Record what you decide with "
            "[cyan]main.py decide ID approve|reject|later[/cyan]."
        )


def cmd_decide(args: argparse.Namespace) -> None:
    """Record a decision on a recommendation, or list what is pending.

    Approving is not executing. The gap is deliberate: the strategy asks for a
    cooling-off period, so the decision and the trade are separate acts.

    Raises:
        ValueError: If the recommendation is unknown or already expired.
        ProfileConfigError: If the profile or its database is missing.
    """
    from datetime import UTC, datetime

    from rich.console import Console
    from rich.table import Table

    console = Console()
    _, db_path = resolve_cli_profile(args.profile, db=args.db)
    conn = open_existing_db(db_path)
    today = date.today().isoformat()

    if args.recommendation_id is None:
        rows = conn.execute(
            """
            SELECT r.id, r.run_date, r.action, r.amount_eur, r.expires_on,
                   r.rationale, s.ticker,
                   (SELECT decision FROM user_decision d
                     WHERE d.recommendation_id = r.id
                     ORDER BY d.id DESC LIMIT 1) AS decision
            FROM recommendation r
            LEFT JOIN securities s ON s.id = r.security_id
            ORDER BY r.id DESC LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        if not rows:
            console.print("No recommendations yet. Run [cyan]main.py recommend[/cyan].")
            return

        table = Table(title="Recommendations")
        table.add_column("ID", justify="right")
        table.add_column("Date", style="dim")
        table.add_column("Action", style="bold")
        table.add_column("Ticker")
        table.add_column("Amount", justify="right")
        table.add_column("Status")
        for row in rows:
            expired = row["expires_on"] < today and not row["decision"]
            status = (
                "[dim]expired[/dim]"
                if expired
                else (row["decision"] or "[yellow]pending[/yellow]")
            )
            table.add_row(
                str(row["id"]),
                row["run_date"],
                row["action"],
                row["ticker"] or "—",
                f"{row['amount_eur']:,.2f}" if row["amount_eur"] else "—",
                status,
            )
        console.print(table)
        return

    row = conn.execute(
        "SELECT * FROM recommendation WHERE id = ?", (args.recommendation_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"No recommendation with id {args.recommendation_id}.")
    if row["expires_on"] < today:
        raise ValueError(
            f"Recommendation {row['id']} expired on {row['expires_on']}. A weekly "
            f"cadence supersedes itself, so acting on it now would execute "
            f"research that has been replaced, at a price that has moved."
        )

    with conn:
        conn.execute(
            """
            INSERT INTO user_decision (recommendation_id, decision, note, decided_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                row["id"],
                args.decision,
                args.note,
                datetime.now(UTC).isoformat(),
            ),
        )
    console.print(
        f"[green]Recorded[/green] {args.decision} on recommendation {row['id']} "
        f"({row['action']})."
    )
    if args.decision == "approve":
        console.print(
            "[dim]Nothing has been traded. Execute it yourself, then record it "
            "with 'main.py executed ID'.[/dim]"
        )
