"""The child process that runs one chart's code.

Its own entry point rather than a ``multiprocessing`` target, for two reasons
found the hard way. Under the spawn start method the child re-imports the
parent's ``__main__``, which is fragile and depends on how the daemon happened
to be launched; and ``fork`` is unsafe in a process that already has threads,
which the daemon does. A plain subprocess reading stdin and writing stdout
depends on neither.

Reads ``{"code": ..., "rows": [...]}`` as JSON on stdin. Writes PNG bytes to
stdout and exits 0, or writes the reason to stderr and exits 1. Nothing else is
printed to stdout, because stdout *is* the image.

Example:
    echo '{"code": "...", "rows": []}' | python -m chart_worker > chart.png
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    """Render one chart from a JSON request on stdin.

    Returns:
        0 when a PNG was written to stdout, 1 otherwise.
    """
    try:
        request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        code = str(request["code"])
        rows = list(request.get("rows") or [])
    except Exception as exc:  # noqa: BLE001 - the parent reads this message
        sys.stderr.write(f"unreadable chart request: {exc}")
        return 1

    try:
        from charts import build_namespace, figure_to_png

        namespace = build_namespace(rows)
        exec(code, namespace)  # noqa: S102 - running the model's chart is the job
        figure = namespace.get("fig")
        if figure is None:
            sys.stderr.write("the code left no 'fig'")
            return 1
        sys.stdout.buffer.write(figure_to_png(figure))
        sys.stdout.buffer.flush()
    except BaseException as exc:  # noqa: BLE001 - every failure is the parent's to log
        sys.stderr.write(f"{type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
