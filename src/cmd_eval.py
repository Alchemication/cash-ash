"""Report whether the machinery is sound, since the outcomes cannot be judged."""

from __future__ import annotations

import argparse
import logging

from profiles import resolve_cli_profile
from store import open_existing_db

logger = logging.getLogger(__name__)


def cmd_eval(args: argparse.Namespace) -> None:
    """Run the process invariants and print the descriptive metrics.

    Raises:
        ProfileConfigError: If the profile or its database is missing.
    """
    from rich.console import Console
    from rich.table import Table

    from evals import observe, run_checks

    console = Console()
    _, db_path = resolve_cli_profile(args.profile, db=args.db)
    conn = open_existing_db(db_path)

    checks = run_checks(conn)
    failed = [check for check in checks if not check.passed]

    table = Table(title="Invariants")
    table.add_column("", justify="center")
    table.add_column("Check", style="bold")
    table.add_column("Detail", style="dim", overflow="fold")
    for check in checks:
        mark = "[green]ok[/green]" if check.passed else "[red]broken[/red]"
        detail = check.why if not check.passed else ""
        if check.offenders:
            detail = f"{', '.join(check.offenders[:8])} — {detail}"
        table.add_row(mark, check.name, detail)
    console.print(table)

    metrics = Table(title="Observations — activity windows stated in each measure")
    metrics.add_column("Measure", style="bold")
    metrics.add_column("Value")
    metrics.add_column("What it does not mean", style="dim", overflow="fold")
    for item in observe(conn, weeks=args.weeks):
        metrics.add_row(item.name, item.value, item.note)
    console.print(metrics)

    console.print(
        "[dim]Observations carry no verdict on purpose. Fourteen holdings a "
        "week cannot show that this system picks stocks well, and a threshold "
        "nobody can justify is worse than an honest number.[/dim]"
    )
    if failed:
        console.print(
            f"[red]{len(failed)} invariant(s) broken.[/red] These are defects, "
            f"not preferences — each has no tolerable rate."
        )
        raise SystemExit(1)
