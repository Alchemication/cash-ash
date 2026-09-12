"""Charts the chat agent draws, from code it writes itself.

The model emits a ``<chart>`` block holding plotly code; this extracts it, runs
it, and returns a PNG for Telegram. Writing code is the right interface here
rather than filling in a chart specification: a question like "show me my weights
against the limit" wants a reference line, "pace of contributions" wants a
cumulative sum, and enumerating those in advance produces a worse chart than
letting the model compute what it needs from the rows it already fetched.

Running model-written code is the cost of that, so it runs in a separate process
that can be killed, in a namespace with no file, process or network access: ``open``, ``exec``, ``eval``, ``compile`` and
``input`` are gone, and imports are allowlisted to the plotting and maths
modules. The allowlist rather than removing ``__import__`` altogether, because
every chart the model writes begins by importing plotly, and breaking that to
close a hole nobody can reach — the code comes from a model answering a question
from one allowlisted person on their own machine — would trade a working feature
for a theoretical one.

A chart that fails to render falls back to a plain line or bar drawn from the
rows by ``rows_chart``, then to nothing at all. The answer's text never depends
on the picture arriving, so losing it costs detail and not meaning.

Public API:
    ChartBlock       -- one extracted chart
    extract_charts   -- pull ``<chart>`` blocks out of a reply
    strip_charts     -- remove them, leaving the prose
    render_chart     -- run one block's code, returning PNG bytes
    rows_chart       -- draw rows directly, when the code failed
    wants_a_chart    -- whether a question was asking to see one
    build_namespace  -- the namespace chart code runs in, for the worker
    figure_to_png    -- render a figure, for the worker

Example:
    from charts import extract_charts, render_chart, strip_charts

    for block in extract_charts(reply):
        image = render_chart(block.code, rows=rows)
    text = strip_charts(reply)
"""

from __future__ import annotations

import builtins
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from config import (
    CHART_EXEC_TIMEOUT_S,
    CHART_HEIGHT_PX,
    CHART_WIDTH_PX,
)

logger = logging.getLogger(__name__)

_CHART_RE = re.compile(
    r'<chart(?:\s+title="([^"]*)")?\s*>(.*?)</chart>', re.DOTALL | re.IGNORECASE
)
"""A chart block and its optional title."""

_STRIP_RE = re.compile(r"\s*<chart[^>]*>.*?</chart>\s*", re.DOTALL | re.IGNORECASE)
"""The same blocks plus their surrounding whitespace, for removal."""

ALLOWED_IMPORTS: frozenset[str] = frozenset(
    {"plotly", "plotly.graph_objects", "plotly.express", "numpy", "math", "datetime"}
)
"""Modules chart code may import.

Everything a chart legitimately needs and nothing that reaches the filesystem,
a subprocess or the network. Submodules of an allowed package are allowed with
it; anything else raises inside the chart and loses only the picture.
"""

_BANNED_BUILTINS: frozenset[str] = frozenset(
    {"open", "exec", "eval", "compile", "input", "breakpoint", "memoryview"}
)
"""Builtins removed from chart code.

``__import__`` is deliberately not here — it is wrapped instead, because chart
code opens by importing plotly.
"""

_PREFERRED_X: tuple[str, ...] = (
    "ticker",
    "label",
    "entry_date",
    "trade_date",
    "flow_date",
    "price_date",
    "date",
    "sector",
)
_PREFERRED_Y: tuple[str, ...] = (
    "weight_pct",
    "value_eur",
    "delta_eur",
    "amount_eur",
    "cost_basis_eur",
    "unrealised_return_pct",
    "close_native",
    "quantity",
)
"""Columns the fallback chart prefers, most-asked-about first."""

_DATE_KEYS = ("date",)
"""Substring marking an x column that should be drawn as a line, not bars."""


@dataclass(frozen=True)
class ChartBlock:
    """One chart the model asked for."""

    title: str
    code: str


def extract_charts(text: str) -> list[ChartBlock]:
    """Return every ``<chart>`` block in *text*, in order."""
    return [
        ChartBlock(title=(title or "").strip(), code=code.strip())
        for title, code in _CHART_RE.findall(text)
        if code.strip()
    ]


def strip_charts(text: str) -> str:
    """Return *text* with chart blocks removed and whitespace tidied."""
    return _STRIP_RE.sub("\n\n", text).strip()


def wants_a_chart(question: str) -> bool:
    """Return True when a question was asking to be shown something.

    Used only to draw a fallback chart the model did not ask for, so a false
    positive costs one unnecessary picture and a false negative costs nothing.
    """
    lowered = question.lower()
    return any(
        marker in lowered
        for marker in (
            "chart",
            "graph",
            "plot",
            "show me",
            "visualis",
            "visualiz",
            "over time",
            "breakdown",
            "split",
        )
    )


def _guarded_import(name: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
    """Import *name* only if it is allowlisted.

    Raises:
        ImportError: For anything not in ``ALLOWED_IMPORTS``, which surfaces
            inside the chart code and costs only the chart.
    """
    root = name.split(".")[0]
    if name in ALLOWED_IMPORTS or root in ALLOWED_IMPORTS:
        return builtins.__import__(name, *args, **kwargs)  # type: ignore[arg-type]
    raise ImportError(
        f"Chart code may not import {name}. Allowed: {', '.join(sorted(ALLOWED_IMPORTS))}."
    )


def build_namespace(rows: tuple[dict, ...] | list[dict]) -> dict:
    """Build the namespace chart code runs in."""
    import numpy as np
    import plotly.express as px
    import plotly.graph_objects as go

    safe = {
        name: value
        for name, value in vars(builtins).items()
        if name not in _BANNED_BUILTINS
    }
    safe["__import__"] = _guarded_import
    return {
        "__builtins__": safe,
        "go": go,
        "px": px,
        "np": np,
        "rows": [dict(row) for row in rows],
    }


def figure_to_png(figure: object) -> bytes:
    """Render a plotly figure to PNG bytes at a phone-readable size."""
    return figure.to_image(  # type: ignore[attr-defined]
        format="png", width=CHART_WIDTH_PX, height=CHART_HEIGHT_PX, scale=2
    )


def render_chart(
    code: str, *, rows: tuple[dict, ...] | list[dict] = ()
) -> bytes | None:
    """Run chart code in a child process and return a PNG.

    A separate process because it is the only way to stop the code: a thread
    cannot be killed, so a runaway loop abandoned on one goes on burning a core
    for the life of the daemon and starves every later chart past its own
    timeout — observed, and the reason this is not a thread. A process can be
    killed, and a crash inside a rendering library takes the child rather than
    the daemon.

    Args:
        code: Python the model wrote. Must leave a figure in ``fig``.
        rows: Tool rows, available to the code as ``rows``.

    Returns:
        PNG bytes, or None if the code failed, timed out, or drew nothing.
    """
    payload = json.dumps(
        {"code": code, "rows": [dict(row) for row in rows]}, default=str
    ).encode("utf-8")
    environment = {
        **os.environ,
        # The child imports this module by name, so it needs src on the path
        # however the parent itself was started.
        "PYTHONPATH": os.pathsep.join(
            filter(
                None,
                [str(Path(__file__).resolve().parent), os.environ.get("PYTHONPATH")],
            )
        ),
    }
    try:
        finished = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-m", "chart_worker"],
            input=payload,
            capture_output=True,
            timeout=CHART_EXEC_TIMEOUT_S,
            env=environment,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "Chart code did not finish within %ss; the process was killed",
            CHART_EXEC_TIMEOUT_S,
        )
        return None
    except OSError as exc:
        logger.warning("Could not start the chart process: %s", exc)
        return None

    if finished.returncode != 0 or not finished.stdout:
        reason = finished.stderr.decode("utf-8", errors="replace").strip()
        logger.warning("Chart code failed: %s", reason or "no output")
        return None
    return finished.stdout


def _axes(rows: list[dict]) -> tuple[str, str] | None:
    """Choose an x and y column for a fallback chart, or None if none fits."""
    sample = rows[0]
    x = next((key for key in _PREFERRED_X if key in sample), None)
    if x is None:
        x = next((key for key, value in sample.items() if isinstance(value, str)), None)
    if x is None:
        return None
    y = next(
        (
            key
            for key in _PREFERRED_Y
            if key in sample and isinstance(sample[key], int | float)
        ),
        None,
    )
    if y is None:
        y = next(
            (
                key
                for key, value in sample.items()
                if key != x and isinstance(value, int | float)
            ),
            None,
        )
    return None if y is None else (x, y)


def rows_chart(rows: tuple[dict, ...] | list[dict], *, title: str = "") -> bytes | None:
    """Draw rows directly, for when the model's own chart code failed.

    Deliberately dumb: pick the most chart-like pair of columns, a line for a
    date axis and bars otherwise. A plain chart of the right data beats no chart,
    and beats a clever one nobody can check.

    Args:
        rows: Tool rows.
        title: Optional title.

    Returns:
        PNG bytes, or None when the rows are not chartable.
    """
    usable = [dict(row) for row in rows]
    if not usable:
        return None
    axes = _axes(usable)
    if axes is None:
        return None
    x_key, y_key = axes

    # Rows carrying no value at all are dropped rather than drawn as zero: an
    # unpriced holding must not appear as a bar of height nothing.
    points = [
        (row[x_key], row[y_key])
        for row in usable
        if row.get(x_key) is not None and isinstance(row.get(y_key), int | float)
    ]
    if not points:
        return None

    try:
        import plotly.graph_objects as go

        label = y_key.replace("_", " ")
        is_series = any(marker in x_key for marker in _DATE_KEYS)
        figure = go.Figure(
            go.Scatter(
                x=[point[0] for point in points],
                y=[point[1] for point in points],
                mode="lines+markers",
                name=label,
            )
            if is_series
            else go.Bar(
                x=[point[0] for point in points],
                y=[point[1] for point in points],
                name=label,
            )
        )
        figure.update_layout(
            title=title or label.title(),
            xaxis_title="",
            yaxis_title=label,
            margin={"l": 55, "r": 25, "t": 50, "b": 45},
            showlegend=False,
        )
        return figure_to_png(figure)
    except Exception as exc:  # noqa: BLE001 - the answer's text does not need this
        logger.warning("Fallback chart failed: %s", exc)
        return None
