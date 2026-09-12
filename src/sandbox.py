"""Run the real pipeline against a throwaway copy of the database.

A stage that cannot be observed without changing what it observes is hard to
learn from. ``research`` writes a run, evidence and an assessment; ``recommend``
retires every recommendation the owner has not yet ruled on. Both are correct
for the weekly cycle and wrong for someone trying to watch the machinery work,
or to ask what a different model would have concluded.

The sandbox answers that by cloning the database into memory and letting the
unmodified production code write to the clone. Nothing branches on a flag, so
no stage can leak a write by forgetting to check one, and a stage added later
is sandboxed without being told about the sandbox.

Model calls are the exception, and deliberately so. They cost real money and
happen whether or not their result is kept, so their log rows are copied back
into the real database when the block exits — including when it exits on an
error, which is exactly when the record is most worth having. ``llm_trace`` and
``llm_call`` carry no foreign key into the rest of the schema, so the copy is
exact rather than approximate.

Public API:
    sandboxed -- context manager yielding an in-memory clone

Example:
    from sandbox import sandboxed

    with sandboxed(db_path) as conn:
        research_security(conn, ticker="AMD")
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

LOG_TABLES: tuple[str, ...] = ("llm_trace", "llm_call")
"""Tables whose rows survive a sandbox run.

Both are standalone by design — ``llm_call`` deliberately carries no foreign key
to ``research_run`` — so rows can be copied into a database that never saw the
run they describe.
"""


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return a table's column names except ``id``.

    Read from the live schema rather than hardcoded, so a migration that adds a
    column does not silently stop copying it. Table names come from
    :data:`LOG_TABLES`, never from user input.
    """
    return [
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table})")
        if row["name"] != "id"
    ]


def _persist_log(
    clone: sqlite3.Connection, db_path: Path, *, after: dict[str, int]
) -> int:
    """Copy model-call rows created in *clone* into the real database.

    Trace ids are reassigned on the way in, because the real database may have
    allocated the same numbers to a different operation meanwhile. Calls
    belonging to a trace that predates the sandbox keep the id they had.

    Args:
        clone: The sandbox connection.
        db_path: The real database.
        after: Highest id per log table before the run.

    Returns:
        How many calls were recorded.
    """
    traces = clone.execute(
        "SELECT * FROM llm_trace WHERE id > ? ORDER BY id", (after["llm_trace"],)
    ).fetchall()
    calls = clone.execute(
        "SELECT * FROM llm_call WHERE id > ? ORDER BY id", (after["llm_call"],)
    ).fetchall()
    if not traces and not calls:
        return 0

    # Raw connect on purpose: the database was migrated when the sandbox opened
    # it, and writing a log row must never be able to apply a schema change.
    target = sqlite3.connect(str(db_path))
    target.row_factory = sqlite3.Row
    try:
        trace_columns = _columns(clone, "llm_trace")
        call_columns = _columns(clone, "llm_call")
        remapped: dict[int, int] = {}
        with target:
            for row in traces:
                cursor = target.execute(
                    f"INSERT INTO llm_trace ({','.join(trace_columns)}) "
                    f"VALUES ({','.join('?' * len(trace_columns))})",
                    [row[column] for column in trace_columns],
                )
                remapped[int(row["id"])] = int(cursor.lastrowid or 0)
            for row in calls:
                values = [row[column] for column in call_columns]
                trace_id = row["trace_id"]
                if trace_id is not None:
                    values[call_columns.index("trace_id")] = remapped.get(
                        int(trace_id), int(trace_id)
                    )
                target.execute(
                    f"INSERT INTO llm_call ({','.join(call_columns)}) "
                    f"VALUES ({','.join('?' * len(call_columns))})",
                    values,
                )
    finally:
        target.close()
    return len(calls)


@contextmanager
def sandboxed(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Yield an in-memory clone of the database, keeping only the model log.

    The clone is discarded when the block ends. Rows written to ``llm_trace``
    and ``llm_call`` are copied into the real database first, whether the block
    finished or raised.

    Args:
        db_path: The real database. It is opened once, migrated if it is behind,
            and thereafter read only to take the copy.

    Yields:
        A connection to the clone, indistinguishable from the real one to
        everything downstream.

    Raises:
        FileNotFoundError: If no database exists at *db_path*.
    """
    from store import open_existing_db

    # Migrate first, so the clone is never a copy of an out-of-date schema.
    open_existing_db(db_path).close()

    resolved = db_path.expanduser().resolve()
    source = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    clone = sqlite3.connect(":memory:")
    try:
        source.backup(clone)
    finally:
        source.close()

    clone.row_factory = sqlite3.Row
    clone.execute("PRAGMA foreign_keys = ON")
    after = {
        table: int(
            clone.execute(
                f"SELECT COALESCE(MAX(id), 0) AS top FROM {table}"
            ).fetchone()["top"]
        )
        for table in LOG_TABLES
    }

    try:
        yield clone
    finally:
        try:
            recorded = _persist_log(clone, resolved, after=after)
            if recorded:
                logger.info("Recorded %d sandbox model call(s) in the log", recorded)
        except sqlite3.Error as exc:
            # The run already happened and already cost money; losing its log is
            # bad, but failing the command the owner is reading is worse.
            logger.warning("Could not record sandbox model calls: %s", exc)
        finally:
            clone.close()


def sandbox_cost(
    conn: sqlite3.Connection, *, trace_id: int | None
) -> tuple[int, float]:
    """Return the call count and known cost for one trace.

    Args:
        conn: Connection the calls were logged through.
        trace_id: Trace to total, or None when the operation recorded none.

    Returns:
        ``(calls, cost in USD)``. Cost is 0.0 when no call reported one.
    """
    if trace_id is None:
        return 0, 0.0
    row = conn.execute(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS cost "
        "FROM llm_call WHERE trace_id = ?",
        (trace_id,),
    ).fetchone()
    return int(row["calls"]), float(row["cost"])


def run_routing(model: str | None, *, no_store: bool) -> dict[str, str] | None:
    """Validate a ``--model`` argument against the sandbox flag.

    A routing override is allowed only where nothing is kept. A stored run must
    always match what ``main.py models`` reports, or the routing table stops
    being an explanation of how a recorded result was produced.

    Args:
        model: The raw ``--model`` argument, or None.
        no_store: Whether this run discards its writes.

    Returns:
        Model by feature, or None when no override was given.

    Raises:
        ValueError: If an override was given for a run that stores its results.
    """
    from model_prefs import parse_overrides

    if model is None:
        return None
    if not no_store:
        raise ValueError(
            "--model applies only to a --no-store run, so a stored result always "
            "matches the routing table. To change a real route, use "
            "'main.py models set FEATURE --model MODEL'."
        )
    return parse_overrides(model)
