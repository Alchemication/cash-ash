"""Install, stop and restart the background listener under launchd."""

from __future__ import annotations

import argparse
import logging
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from config import DAEMON_LOG_FILE, LAUNCHD_LABEL

logger = logging.getLogger(__name__)

_PLIST_DIR = Path.home() / "Library" / "LaunchAgents"


def _plist_path() -> Path:
    """Return where the launchd job description lives."""
    return _PLIST_DIR / f"{LAUNCHD_LABEL}.plist"


def _render_plist(project: Path) -> dict:
    """Build the launchd job for this checkout.

    ``KeepAlive`` restarts the listener whenever it exits, which is what makes
    it survive the machine sleeping — a suspended process is resumed rather
    than restarted, but a connection dropped while asleep kills the poll, and
    without this the daemon would simply stay dead until noticed by hand.

    ``RunAtLoad`` starts it on login so there is nothing to remember.

    Args:
        project: Path to the checkout.

    Returns:
        The plist as a dictionary.
    """
    log = DAEMON_LOG_FILE
    log.parent.mkdir(parents=True, exist_ok=True)
    return {
        "Label": LAUNCHD_LABEL,
        # Invoked through uv rather than the venv's python directly, matching
        # zdrowskit. uv re-resolves dependencies at start, so a venv rebuilt or
        # a dependency added does not leave a job pointing at a stale
        # interpreter that fails only at the next restart.
        "ProgramArguments": [
            _uv_path(),
            "run",
            "python",
            str(project / "main.py"),
            "daemon",
        ],
        "WorkingDirectory": str(project),
        "RunAtLoad": True,
        # Restart on a crash, but not on a clean exit. The daemon exits cleanly
        # when there is no bot token, and an unconditional KeepAlive would
        # relaunch it into the same misconfiguration every thirty seconds.
        "KeepAlive": {"SuccessfulExit": False},
        # Throttle a crash loop: without it a daemon failing at startup is
        # relaunched as fast as launchd can manage.
        "ThrottleInterval": 30,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "EnvironmentVariables": _launchd_environment(),
    }


def _uv_path() -> str:
    """Return an absolute path to uv, which launchd cannot find on PATH."""
    found = shutil.which("uv")
    if found:
        return found
    for candidate in (
        Path.home() / ".local" / "bin" / "uv",
        Path("/opt/homebrew/bin/uv"),
        Path("/usr/local/bin/uv"),
    ):
        if candidate.exists():
            return str(candidate)
    raise ValueError(
        "Cannot find uv. launchd runs with a minimal PATH and needs an "
        "absolute path to it, so install uv or run the daemon in the "
        "foreground instead."
    )


def _launchd_environment() -> dict[str, str]:
    """Return the environment a launchd job needs.

    launchd starts jobs with almost nothing set. HOME in particular is absent,
    and every user-owned path in this project is derived from it — an app home
    resolved against a missing HOME lands somewhere unintended rather than
    failing loudly. PATH carries the Homebrew locations because the default
    omits them entirely on Apple Silicon.
    """
    home = Path.home()
    return {
        "HOME": str(home),
        "PATH": ":".join(
            [
                str(home / ".local" / "bin"),
                "/opt/homebrew/bin",
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
            ]
        ),
    }


def _launchctl(*args: str) -> tuple[int, str]:
    """Run launchctl, returning its exit code and combined output."""
    result = subprocess.run(
        ["launchctl", *args], capture_output=True, text=True, check=False
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def cmd_daemon(args: argparse.Namespace) -> None:
    """Run the listener in the foreground, or manage its launchd job.

    Raises:
        ValueError: If the platform does not support launchd.
        TelegramError: If another poller is already running.
    """
    from rich.console import Console

    console = Console()

    if args.daemon_cmd is None:
        from daemon import run_daemon

        console.print(
            "[dim]Listening for button presses and commands. Ctrl-C to stop.[/dim]"
        )
        run_daemon()
        return

    if sys.platform != "darwin":
        raise ValueError(
            f"launchd is macOS-only and this is {sys.platform}. Run "
            f"'main.py daemon' under your own supervisor instead."
        )

    path = _plist_path()

    if args.daemon_cmd == "install":
        project = Path(__file__).resolve().parent.parent
        _PLIST_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps(_render_plist(project)))
        _launchctl("unload", str(path))
        code, output = _launchctl("load", str(path))
        if code != 0:
            raise ValueError(f"launchctl load failed: {output}")
        console.print(f"[green]Installed[/green] {LAUNCHD_LABEL}")
        console.print(f"[dim]{path}[/dim]")
        console.print(f"[dim]Logs: {DAEMON_LOG_FILE}[/dim]")
        return

    if not path.exists():
        raise ValueError(
            f"No launchd job at {path}. Run 'main.py daemon install' first."
        )

    if args.daemon_cmd == "stop":
        code, output = _launchctl("unload", str(path))
        console.print(
            f"[green]Stopped[/green] {LAUNCHD_LABEL}"
            if code == 0
            else f"[yellow]{output}[/yellow]"
        )
        return

    _launchctl("unload", str(path))
    code, output = _launchctl("load", str(path))
    if code != 0:
        raise ValueError(f"launchctl load failed: {output}")
    console.print(f"[green]Restarted[/green] {LAUNCHD_LABEL}")
