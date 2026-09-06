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
    """Every subcommand the CLI exposes must appear in docs/commands.md."""

    def test_documents_every_subcommand(self) -> None:
        main_source = (ROOT / "main.py").read_text()
        documented = (ROOT / "docs" / "commands.md").read_text()
        commands = set(re.findall(r'sub\.add_parser\(\s*"([a-z-]+)"', main_source))
        assert commands, "No subcommands found in main.py; the regex is stale."
        missing = sorted(
            name for name in commands if f"main.py {name}" not in documented
        )
        assert not missing, f"Undocumented subcommands: {missing}"


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
