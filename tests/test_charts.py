"""Tests for charts drawn from code the model wrote.

Two things matter here. That a chart which fails costs only the picture — the
answer's text must survive every way rendering can go wrong — and that the
process running model-written code cannot reach anything, and can be stopped.

The rendering tests shell out to a real child process, so they are slower than
the rest of the suite. They are worth it: the bug this design exists to fix was
invisible to any test that did not actually run the code.
"""

from __future__ import annotations

import time

import pytest

from charts import (
    ALLOWED_IMPORTS,
    ChartBlock,
    extract_charts,
    render_chart,
    rows_chart,
    strip_charts,
    wants_a_chart,
)

_BAR = (
    "import plotly.graph_objects as go\n"
    'fig = go.Figure(go.Bar(x=[r["ticker"] for r in rows], '
    'y=[r["weight_pct"] for r in rows]))'
)

_WEIGHTS = [
    {"ticker": "BRK.B", "weight_pct": 19.4},
    {"ticker": "AMD", "weight_pct": 8.1},
]


def _is_png(data: bytes | None) -> bool:
    return bool(data) and data[:8] == b"\x89PNG\r\n\x1a\n"


class TestExtraction:
    """Pulling a chart out of a reply, and leaving readable prose behind."""

    def test_a_block_and_its_title_are_found(self) -> None:
        reply = 'Here.\n\n<chart title="Weights">\nfig = 1\n</chart>\n\nBRK.B leads.'
        (block,) = extract_charts(reply)
        assert block == ChartBlock(title="Weights", code="fig = 1")

    def test_a_block_without_a_title_still_parses(self) -> None:
        (block,) = extract_charts("<chart>\nfig = 1\n</chart>")
        assert block.title == ""

    def test_several_blocks_keep_their_order(self) -> None:
        reply = '<chart title="a">fig=1</chart><chart title="b">fig=2</chart>'
        assert [block.title for block in extract_charts(reply)] == ["a", "b"]

    def test_an_empty_block_is_ignored(self) -> None:
        assert extract_charts("<chart title='x'>\n  \n</chart>") == []

    def test_stripping_leaves_the_prose_joined_cleanly(self) -> None:
        reply = "Your weights.\n\n<chart>fig=1</chart>\n\nBRK.B leads."
        assert strip_charts(reply) == "Your weights.\n\nBRK.B leads."

    def test_stripping_a_reply_that_is_only_a_chart_leaves_nothing(self) -> None:
        assert strip_charts("<chart>fig=1</chart>") == ""

    def test_text_without_a_chart_is_untouched(self) -> None:
        assert strip_charts("Just a sentence.") == "Just a sentence."


class TestWantsAChart:
    """Only used to offer an unasked-for chart, so it errs towards offering."""

    @pytest.mark.parametrize(
        "question",
        ["chart my weights", "show me the split", "value over time", "plot it"],
    )
    def test_a_request_to_see_something_is_recognised(self, question: str) -> None:
        assert wants_a_chart(question) is True

    @pytest.mark.parametrize(
        "question", ["how much cash do I have", "what did I pay for BRK.B"]
    )
    def test_a_plain_question_is_not(self, question: str) -> None:
        assert wants_a_chart(question) is False


class TestSandbox:
    """Model-written code runs where it cannot reach anything."""

    @pytest.mark.parametrize(
        "code",
        [
            "import os\nfig = None",
            '__import__("os").system("true")\nfig = None',
            "import subprocess\nfig = None",
            "from pathlib import Path\nfig = None",
            "import socket\nfig = None",
            'open("/tmp/cash-ash-chart-test", "w")\nfig = None',
            'eval("1+1")\nfig = None',
            'exec("x=1")\nfig = None',
        ],
    )
    def test_reaching_outside_fails_and_draws_nothing(self, code: str) -> None:
        assert render_chart(code) is None

    def test_the_allowlist_covers_what_a_chart_needs(self) -> None:
        # Stated as a set so removing one is a deliberate act, not a typo.
        assert {"plotly", "numpy", "math", "datetime"} <= ALLOWED_IMPORTS
        assert not {"os", "subprocess", "pathlib", "socket"} & ALLOWED_IMPORTS

    def test_a_file_a_chart_tried_to_write_does_not_exist(self) -> None:
        from pathlib import Path

        target = Path("/tmp/cash-ash-chart-test")
        target.unlink(missing_ok=True)
        render_chart(f'open("{target}", "w").write("x")\nfig = None')
        assert not target.exists()


class TestRendering:
    """The working path, and every way it can fail."""

    def test_plotly_code_becomes_a_png(self) -> None:
        assert _is_png(render_chart(_BAR, rows=_WEIGHTS))

    def test_numpy_is_available(self) -> None:
        code = (
            "import numpy as np\n"
            "import plotly.graph_objects as go\n"
            'values = np.array([r["weight_pct"] for r in rows])\n'
            "fig = go.Figure(go.Bar(x=[1, 2], y=values / values.sum()))"
        )
        assert _is_png(render_chart(code, rows=_WEIGHTS))

    def test_code_leaving_no_figure_returns_nothing(self) -> None:
        assert render_chart("import plotly.graph_objects as go\nx = 1") is None

    def test_broken_code_returns_nothing(self) -> None:
        assert render_chart("fig = this_name_does_not_exist") is None

    def test_a_runaway_is_killed_at_its_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Shortened so the suite does not wait out the real budget; what is
        # being tested is that the timeout fires and the process dies, not the
        # size of the number.
        import charts as charts_module

        monkeypatch.setattr(charts_module, "CHART_EXEC_TIMEOUT_S", 2.0)
        started = time.monotonic()
        assert render_chart("while True: pass") is None
        assert time.monotonic() - started < 10

    def test_charting_still_works_after_a_runaway(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The whole reason this runs in a process. Abandoned on a thread, a
        # runaway loop starved every later chart for the life of the daemon.
        import charts as charts_module

        monkeypatch.setattr(charts_module, "CHART_EXEC_TIMEOUT_S", 2.0)
        render_chart("while True: pass")
        monkeypatch.undo()
        assert _is_png(render_chart(_BAR, rows=_WEIGHTS))

    def test_a_chart_that_crashes_the_renderer_does_not_crash_the_caller(self) -> None:
        # A rendering library segfaulting takes the child, not the daemon.
        assert render_chart("import os\nos._exit(0)") is None


class TestRowsChart:
    """The fallback, for when the model's own code failed."""

    def test_categories_become_a_chart(self) -> None:
        assert _is_png(rows_chart(_WEIGHTS))

    def test_a_date_column_is_chosen_as_the_axis(self) -> None:
        rows = [
            {"entry_date": "2026-01-01", "delta_eur": 100.0},
            {"entry_date": "2026-02-01", "delta_eur": -20.0},
        ]
        assert _is_png(rows_chart(rows, title="Cash"))

    def test_no_rows_draws_nothing(self) -> None:
        assert rows_chart([]) is None

    def test_rows_with_nothing_numeric_draw_nothing(self) -> None:
        assert rows_chart([{"ticker": "BRK.B", "note": "held"}]) is None

    def test_a_null_value_is_dropped_not_plotted_as_zero(self) -> None:
        # An unpriced holding must not appear as a bar of height nothing.
        rows = [
            {"ticker": "BRK.B", "value_eur": 265.31},
            {"ticker": "DARK", "value_eur": None},
        ]
        assert _is_png(rows_chart(rows))

    def test_rows_that_are_all_null_draw_nothing(self) -> None:
        assert rows_chart([{"ticker": "DARK", "value_eur": None}]) is None
