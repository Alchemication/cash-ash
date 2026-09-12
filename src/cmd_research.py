"""Run a deep research pass on one holding, or on whatever triage selected."""

from __future__ import annotations

import argparse
import logging
import sqlite3
from contextlib import ExitStack

from profiles import resolve_cli_profile
from store import open_existing_db

logger = logging.getLogger(__name__)

_STATUS_STYLE = {
    "improving": "green",
    "unchanged": "cyan",
    "deteriorating": "yellow",
    "broken": "red",
}


def _latest_triage_selection(conn) -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    """Return (ticker, reason) for holdings the most recent triage selected."""
    row = conn.execute(
        "SELECT id FROM research_run WHERE kind = 'triage' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return []
    return [
        (item["ticker"], item["reason"])
        for item in conn.execute(
            """
            SELECT s.ticker, t.reason
            FROM triage_result t
            JOIN securities s ON s.id = t.security_id
            WHERE t.run_id = ? AND t.selected = 1
            ORDER BY t.rank
            """,
            (row["id"],),
        )
    ]


def _trace_of(conn: sqlite3.Connection, run_id: int) -> int | None:
    """Return the model-call trace a research run recorded under."""
    row = conn.execute(
        "SELECT trace_id FROM research_run WHERE id = ?", (run_id,)
    ).fetchone()
    return None if row is None or row["trace_id"] is None else int(row["trace_id"])


def cmd_research(args: argparse.Namespace) -> None:
    """Research one holding, or every holding the last triage selected.

    Raises:
        ValueError: If a ticker or its thesis is missing, a routing override is
            invalid, or output is unusable.
        ProfileConfigError: If the profile or its database is missing.
    """
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    from research import research_security
    from research_evidence import uncovered_questions
    from sandbox import run_routing, sandbox_cost, sandboxed

    console = Console()
    profile, db_path = resolve_cli_profile(args.profile, db=args.db)
    no_store = getattr(args, "no_store", False)
    overrides = run_routing(getattr(args, "model", None), no_store=no_store)

    with ExitStack() as stack:
        conn = (
            stack.enter_context(sandboxed(db_path))
            if no_store
            else open_existing_db(db_path)
        )

        if no_store:
            routed = (
                "; ".join(
                    f"{feature} on {model}" for feature, model in overrides.items()
                )
                if overrides
                else "the configured routing"
            )
            console.print(
                Panel(
                    f"Running for real on {routed}. The models are called and "
                    f"billed, and the call log is kept. Everything else this "
                    f"produces — the run, its evidence, its assessment and any "
                    f"proposed thesis — is discarded when the command ends.",
                    title="no-store run",
                    border_style="magenta",
                )
            )

        if args.ticker:
            targets = [(args.ticker.upper(), "requested directly")]
        else:
            targets = _latest_triage_selection(conn)
            if not targets:
                console.print(
                    "Nothing selected by the last triage, and no ticker given. Run "
                    "[cyan]main.py triage[/cyan] first, or name a holding."
                )
                return
            console.print(
                f"Researching {len(targets)} holding(s) selected by the last "
                f"triage: {', '.join(t for t, _ in targets)}\n"
            )

        calls = 0
        cost = 0.0
        for ticker, trigger in targets:
            console.print(f"[bold]{ticker}[/bold] — {trigger}")
            try:
                result = research_security(
                    conn,
                    ticker=ticker,
                    trigger=trigger,
                    evidence_file=getattr(args, "evidence_file", None)
                    or (profile.context / "evidence.json" if profile else None),
                    model_overrides=overrides,
                )
            except ValueError as exc:
                console.print(f"  [red]{exc}[/red]\n")
                continue

            if no_store:
                made, spent = sandbox_cost(
                    conn, trace_id=_trace_of(conn, result.run_id)
                )
                calls += made
                cost += spent

            style = _STATUS_STYLE.get(result.thesis_status, "dim")
            console.print(
                f"  thesis [{style}]{result.thesis_status}[/{style}] — "
                f"{result.status_reason}"
            )

            table = Table(
                show_header=True, header_style="dim", box=None, padding=(0, 1)
            )
            table.add_column("Q", overflow="fold")
            table.add_column("Finding", overflow="fold")
            table.add_column("Basis")
            for answer in result.answers:
                kind = str(answer.get("kind", "background"))
                colour = {"sourced": "green", "unanswered": "dim"}.get(kind, "yellow")
                table.add_row(
                    str(answer.get("question", ""))[:70],
                    str(answer.get("answer", ""))[:160],
                    f"[{colour}]{kind}[/{colour}]",
                )
            console.print(table)
            for answer in result.answers:
                if answer.get("source_url"):
                    console.print(
                        f"  Source: {answer['source_url']} ({answer['published_date']})"
                    )
                    console.print(f"  Excerpt: {answer.get('supporting_quote', '')}")

            if result.triggered:
                console.print(
                    Panel(
                        "\n".join(f"• {item}" for item in result.triggered),
                        title="stated breaking conditions that have occurred",
                        border_style="red",
                    )
                )
            console.print(
                f"  [dim]{result.evidence_count} claim(s) stored, "
                f"{result.sourced_count} with a source · run {result.run_id}[/dim]"
                if not no_store
                else f"  [dim]{result.evidence_count} claim(s) and "
                f"{result.sourced_count} source(s) would have been stored · "
                f"sandbox run {result.run_id}[/dim]"
            )
            gaps = uncovered_questions(list(result.questions), list(result.answers))
            if gaps:
                console.print(
                    Panel(
                        "\n".join(f"• {question}" for question in gaps)
                        + "\n\n[dim]Coverage is insufficient until each has a sourced "
                        "answer, so BUY, ADD and EXIT stay refused. Add dated "
                        "excerpts tagged with these questions to "
                        "context/evidence.json and rerun. Research generates a new "
                        "plan each time; if the questions change, use an empty "
                        "questions list on relevant excerpts to match by symbol.[/dim]",
                        title="questions without a sourced answer",
                        border_style="yellow",
                    )
                )
            if result.proposed_version is not None:
                console.print(
                    f"  [yellow]Proposed thesis v{result.proposed_version}.[/yellow] "
                    f"Nothing changed yet — review with "
                    f"[cyan]main.py thesis show {ticker}[/cyan]."
                    if not no_store
                    else f"  [yellow]Would have proposed thesis "
                    f"v{result.proposed_version}.[/yellow] Discarded with the run."
                )
            console.print()

        if no_store:
            console.print(
                f"[magenta]Nothing was stored.[/magenta] {calls} model call(s) "
                f"cost ${cost:.4f} and are in "
                f"[cyan]main.py llm-log[/cyan]."
            )
