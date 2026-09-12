"""Produce and review portfolio recommendations."""

from __future__ import annotations

import argparse
import logging
from contextlib import ExitStack
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
        ValueError: If there is nothing to decide on, a routing override is
            invalid, or output is unusable.
        ProfileConfigError: If the profile or its database is missing.
    """
    from sandbox import run_routing, sandboxed

    profile, db_path = resolve_cli_profile(args.profile, db=args.db)
    no_store = getattr(args, "no_store", False)
    overrides = run_routing(getattr(args, "model", None), no_store=no_store)

    with ExitStack() as stack:
        conn = (
            stack.enter_context(sandboxed(db_path))
            if no_store
            else open_existing_db(db_path)
        )
        _recommend(conn, profile=profile, no_store=no_store, overrides=overrides)


def _recommend(  # type: ignore[no-untyped-def]
    conn,
    *,
    profile,
    no_store: bool,
    overrides: dict[str, str] | None,
) -> None:
    """Render one decision run. Split out so the connection choice stays readable."""
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    from decisions import build_guardrail_context, run_decision
    from sandbox import sandbox_cost

    console = Console()
    context = build_guardrail_context(conn, profile=profile)

    if no_store:
        routed = (
            "; ".join(f"{feature} on {model}" for feature, model in overrides.items())
            if overrides
            else "the configured routing"
        )
        console.print(
            Panel(
                f"Running for real on {routed}. The model is called and billed, "
                f"and the call log is kept. No recommendation is written, and "
                f"nothing you have yet to approve or reject is superseded.",
                title="no-store run",
                border_style="magenta",
            )
        )

    console.print(
        f"Capital available: [bold]EUR {context.available_capital_eur:,.2f}[/bold] "
        f"({context.cash_eur:,.2f} cash; {context.reserved_cash_eur:,.2f} reserved). "
        f"Future monthly plan: EUR {context.monthly_contribution_eur:,.2f}; not funded.\n"
    )

    run_id, recommendations, summary = run_decision(
        conn, profile=profile, model_overrides=overrides
    )

    live = [item for item in recommendations if not item["refused"]]
    refused = [item for item in recommendations if item["refused"]]

    if not live:
        console.print(
            "No actionable recommendations. Inspect any refusals and review health below."
        )
    else:
        table = Table(title=f"Recommendations — run {run_id}")
        table.add_column("ID", justify="right")
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
                str(item["id"]),
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
    if no_store:
        trace = conn.execute(
            "SELECT trace_id FROM research_run WHERE id = ?", (run_id,)
        ).fetchone()
        calls, spent = sandbox_cost(
            conn, trace_id=None if trace is None else trace["trace_id"]
        )
        console.print(
            f"\n[magenta]Nothing was stored.[/magenta] No recommendation exists "
            f"to decide on, and nothing pending was superseded. {calls} model "
            f"call(s) cost ${spent:.4f} and are in [cyan]main.py llm-log[/cyan]."
        )
    elif live:
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
                   r.rationale, r.superseded_by_run_id, s.ticker,
                   (SELECT snoozed_until FROM user_decision d WHERE d.recommendation_id=r.id ORDER BY d.id DESC LIMIT 1) AS snoozed_until,
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
            # Ordered by what the reader most needs to know: a decision they
            # made, then a replacement they did not, then plain lapsing.
            if row["decision"] == "later":
                status = (
                    f"snoozed until {row['snoozed_until']}"
                    if row["snoozed_until"] and row["snoozed_until"] > today
                    else "pending after snooze"
                )
                if row["superseded_by_run_id"] is not None or row["expires_on"] < today:
                    status = "replaced or expired"
            elif row["decision"]:
                status = row["decision"]
            elif row["superseded_by_run_id"] is not None:
                status = "[dim]replaced by a later run[/dim]"
            elif row["expires_on"] < today:
                status = "[dim]expired[/dim]"
            else:
                status = "[yellow]pending[/yellow]"
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

    from workflow import record_response

    result = record_response(
        conn, args.recommendation_id, args.decision, note=args.note
    )
    console.print(result)
    if args.decision == "approve":
        console.print(
            "[dim]Nothing has been traded. Execute it yourself, then record it "
            "with 'main.py executed ID'.[/dim]"
        )
