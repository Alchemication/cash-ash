"""Guards that documentation cannot silently drift from the code."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


class TestAgentInstructions:
    """CLAUDE.md and AGENTS.md must stay byte-identical."""

    def test_files_are_identical(self) -> None:
        claude = (ROOT / "CLAUDE.md").read_text()
        agents = (ROOT / "AGENTS.md").read_text()
        assert claude == agents, (
            "CLAUDE.md and AGENTS.md have diverged. Edit one, then copy it over "
            "the other."
        )


class TestCommandsDoc:
    """Every subcommand the CLI exposes must appear in docs/commands.md.

    Walks the real parser rather than scraping main.py for string literals:
    the ``db`` subcommands are built in a loop and have no literal to find, so
    a text-matching check would pass without ever seeing them.
    """

    @staticmethod
    def _invocations() -> set[str]:
        import argparse

        from main import build_parser

        def walk(parser: argparse.ArgumentParser, prefix: str = "") -> set[str]:
            found: set[str] = set()
            for action in parser._actions:
                if not isinstance(action, argparse._SubParsersAction):
                    continue
                for name, subparser in action.choices.items():
                    path = f"{prefix}{name}"
                    nested = walk(subparser, prefix=f"{path} ")
                    # Only leaves need documenting; "main.py db" alone is not a
                    # runnable command.
                    found |= nested or {path}
            return found

        return walk(build_parser())

    def test_documents_every_subcommand(self) -> None:
        documented = (ROOT / "docs" / "commands.md").read_text()
        commands = self._invocations()
        assert commands, "No subcommands found; the parser walk is broken."
        missing = sorted(
            name for name in commands if f"main.py {name}" not in documented
        )
        assert not missing, f"Undocumented subcommands: {missing}"

    def test_walk_finds_both_levels(self) -> None:
        # Guards the walk itself: if it silently returned nothing, the check
        # above would pass vacuously.
        commands = self._invocations()
        assert "holdings" in commands
        assert "profile add" in commands
        assert "db migrate" in commands


class TestEnvExample:
    """Every SKARBIE_* override read by config.py must be listed in .env_example."""

    def test_documents_every_override(self) -> None:
        config_source = (ROOT / "src" / "config.py").read_text()
        env_example = (ROOT / ".env_example").read_text()
        names = set(re.findall(r'"(SKARBIE_[A-Z_]+)"', config_source))
        assert names, "No SKARBIE_* variables found in config.py; the regex is stale."
        missing = sorted(name for name in names if name not in env_example)
        assert not missing, f"Undocumented environment variables: {missing}"


class TestConfigConstants:
    """Every public constant in config.py must carry a docstring."""

    @pytest.mark.parametrize("path", [ROOT / "src" / "config.py"])
    def test_constants_are_explained(self, path: Path) -> None:
        source = path.read_text()
        # A module-level constant is documented when a string literal follows
        # its assignment, which is the convention the whole file uses.
        undocumented: list[str] = []
        lines = source.splitlines()
        for index, line in enumerate(lines):
            match = re.match(r"^([A-Z][A-Z0-9_]*)\s*[:=]", line)
            if not match:
                continue
            following = "\n".join(lines[index + 1 : index + 4])
            if not re.search(r'^\s*("""|")', following, re.MULTILINE):
                undocumented.append(match.group(1))
        assert not undocumented, (
            f"config.py constants without a docstring: {undocumented}. Each must "
            f"say how its value was chosen."
        )
