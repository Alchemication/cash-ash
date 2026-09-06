"""Tests for the profile roster, path derivation and context files."""

from __future__ import annotations

from pathlib import Path

import pytest

import profiles as profiles_module
from profiles import (
    CONTEXT_FILES,
    Profile,
    ProfileConfigError,
    add_profile,
    context_status,
    is_stub,
    load_profiles,
    operator_profile,
    resolve_cli_profile,
    validate_profile_name,
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated SKARBIE_HOME with module paths repointed at it."""
    monkeypatch.setattr(profiles_module, "PROFILES_FILE", tmp_path / "profiles.toml")
    monkeypatch.setattr(profiles_module, "PROFILES_DIR", tmp_path / "profiles")
    return tmp_path


def _roster(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


class TestProfileNames:
    """Names become directory names, so they must be filesystem-safe."""

    @pytest.mark.parametrize("name", ["adam", "kasia-2", "a_b", "x1"])
    def test_accepts_safe_names(self, name: str) -> None:
        assert validate_profile_name(name) == name

    @pytest.mark.parametrize("name", ["Adam", "a b", "../etc", "", "ünicode"])
    def test_rejects_unsafe_names(self, name: str) -> None:
        with pytest.raises(ProfileConfigError, match="Invalid profile name"):
            validate_profile_name(name)


class TestProfilePaths:
    """Everything a profile owns hangs off its root."""

    def test_paths_derive_from_root(self, tmp_path: Path) -> None:
        profile = Profile(name="adam", telegram_id=1, root=tmp_path / "adam")
        assert profile.db == tmp_path / "adam" / "portfolio.db"
        assert profile.snapshot == tmp_path / "adam" / "seed_snapshot.toml"
        assert profile.context == tmp_path / "adam" / "context"
        assert profile.context_path("strategy.md").name == "strategy.md"


class TestLoadProfiles:
    """Roster validation."""

    def test_missing_roster_explains_how_to_create_one(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileConfigError, match="profile add"):
            load_profiles(tmp_path / "absent.toml")

    def test_loads_a_valid_roster(self, tmp_path: Path) -> None:
        path = _roster(
            tmp_path / "profiles.toml",
            "[profiles.adam]\ntelegram_id = 1\noperator = true\n"
            "monthly_contribution_eur = 200.0\n",
        )
        profiles = load_profiles(path)
        assert profiles["adam"].telegram_id == 1
        assert profiles["adam"].operator is True
        assert profiles["adam"].monthly_contribution_eur == 200.0

    def test_contribution_defaults_when_absent(self, tmp_path: Path) -> None:
        path = _roster(
            tmp_path / "profiles.toml",
            "[profiles.adam]\ntelegram_id = 1\noperator = true\n",
        )
        assert load_profiles(path)["adam"].monthly_contribution_eur > 0

    def test_empty_roster_is_rejected(self, tmp_path: Path) -> None:
        path = _roster(tmp_path / "profiles.toml", "# nothing here\n")
        with pytest.raises(ProfileConfigError, match="at least one"):
            load_profiles(path)

    def test_non_integer_telegram_id_is_rejected(self, tmp_path: Path) -> None:
        path = _roster(
            tmp_path / "profiles.toml",
            '[profiles.adam]\ntelegram_id = "me"\noperator = true\n',
        )
        with pytest.raises(ProfileConfigError, match="must be an integer"):
            load_profiles(path)

    def test_duplicate_telegram_id_is_rejected(self, tmp_path: Path) -> None:
        path = _roster(
            tmp_path / "profiles.toml",
            "[profiles.adam]\ntelegram_id = 1\noperator = true\n"
            "[profiles.kasia]\ntelegram_id = 1\n",
        )
        with pytest.raises(ProfileConfigError, match="assigned more than once"):
            load_profiles(path)

    def test_two_operators_are_rejected(self, tmp_path: Path) -> None:
        path = _roster(
            tmp_path / "profiles.toml",
            "[profiles.adam]\ntelegram_id = 1\noperator = true\n"
            "[profiles.kasia]\ntelegram_id = 2\noperator = true\n",
        )
        with pytest.raises(ProfileConfigError, match="operator"):
            load_profiles(path)

    def test_no_operator_is_rejected(self, tmp_path: Path) -> None:
        path = _roster(tmp_path / "profiles.toml", "[profiles.adam]\ntelegram_id = 1\n")
        with pytest.raises(ProfileConfigError, match="no operator profile"):
            load_profiles(path)

    def test_negative_contribution_is_rejected(self, tmp_path: Path) -> None:
        path = _roster(
            tmp_path / "profiles.toml",
            "[profiles.adam]\ntelegram_id = 1\noperator = true\n"
            "monthly_contribution_eur = -5.0\n",
        )
        with pytest.raises(ProfileConfigError, match="cannot be negative"):
            load_profiles(path)

    def test_operator_profile_is_returned(self, tmp_path: Path) -> None:
        path = _roster(
            tmp_path / "profiles.toml",
            "[profiles.adam]\ntelegram_id = 1\noperator = true\n"
            "[profiles.kasia]\ntelegram_id = 2\n",
        )
        assert operator_profile(load_profiles(path)).name == "adam"


class TestAddProfile:
    """Creation writes directories, templates and a roster entry."""

    def test_creates_directories_and_templates(self, home: Path) -> None:
        profile = add_profile(
            "adam", 111, operator=True, roster_path=home / "profiles.toml"
        )
        assert profile.context.is_dir()
        for file in CONTEXT_FILES:
            assert profile.context_path(file.name).exists()

    def test_templates_are_marked_as_stubs(self, home: Path) -> None:
        profile = add_profile(
            "adam", 111, operator=True, roster_path=home / "profiles.toml"
        )
        assert all(is_stub(profile.context_path(file.name)) for file in CONTEXT_FILES)

    def test_roster_round_trips(self, home: Path) -> None:
        roster = home / "profiles.toml"
        add_profile(
            "adam",
            111,
            operator=True,
            monthly_contribution_eur=200.0,
            roster_path=roster,
        )
        loaded = load_profiles(roster)
        assert loaded["adam"].telegram_id == 111
        assert loaded["adam"].monthly_contribution_eur == 200.0

    def test_second_profile_appends(self, home: Path) -> None:
        roster = home / "profiles.toml"
        add_profile("adam", 111, operator=True, roster_path=roster)
        add_profile("kasia", 222, roster_path=roster)
        loaded = load_profiles(roster)
        assert set(loaded) == {"adam", "kasia"}
        assert loaded["kasia"].operator is False

    def test_first_profile_must_be_operator(self, home: Path) -> None:
        with pytest.raises(ProfileConfigError, match="first profile must be"):
            add_profile("adam", 111, roster_path=home / "profiles.toml")

    def test_second_operator_is_rejected(self, home: Path) -> None:
        roster = home / "profiles.toml"
        add_profile("adam", 111, operator=True, roster_path=roster)
        with pytest.raises(ProfileConfigError, match="already has an operator"):
            add_profile("kasia", 222, operator=True, roster_path=roster)

    def test_duplicate_name_is_rejected(self, home: Path) -> None:
        roster = home / "profiles.toml"
        add_profile("adam", 111, operator=True, roster_path=roster)
        with pytest.raises(ProfileConfigError, match="already exists"):
            add_profile("adam", 222, roster_path=roster)

    def test_duplicate_telegram_id_is_rejected(self, home: Path) -> None:
        roster = home / "profiles.toml"
        add_profile("adam", 111, operator=True, roster_path=roster)
        with pytest.raises(ProfileConfigError, match="already assigned"):
            add_profile("kasia", 111, roster_path=roster)

    def test_non_positive_telegram_id_is_rejected(self, home: Path) -> None:
        with pytest.raises(ProfileConfigError, match="positive integer"):
            add_profile("adam", 0, operator=True, roster_path=home / "profiles.toml")


class TestResolveCliProfile:
    """Turning --profile/--db into a database path."""

    def test_explicit_db_wins_and_returns_no_profile(
        self, home: Path, tmp_path: Path
    ) -> None:
        db = tmp_path / "custom.db"
        db.touch()
        profile, path = resolve_cli_profile(None, db=str(db))
        assert profile is None
        assert path == db.resolve()

    def test_explicit_missing_db_is_rejected(self, home: Path, tmp_path: Path) -> None:
        with pytest.raises(ProfileConfigError, match="does not exist"):
            resolve_cli_profile(None, db=str(tmp_path / "absent.db"))

    def test_defaults_to_the_operator(self, home: Path) -> None:
        roster = home / "profiles.toml"
        add_profile("adam", 111, operator=True, roster_path=roster)
        add_profile("kasia", 222, roster_path=roster)
        profile, _ = resolve_cli_profile(
            None, roster_path=roster, require_existing=False
        )
        assert profile is not None and profile.name == "adam"

    def test_named_profile_is_selected(self, home: Path) -> None:
        roster = home / "profiles.toml"
        add_profile("adam", 111, operator=True, roster_path=roster)
        add_profile("kasia", 222, roster_path=roster)
        profile, _ = resolve_cli_profile(
            "kasia", roster_path=roster, require_existing=False
        )
        assert profile is not None and profile.name == "kasia"

    def test_unknown_profile_lists_the_known_ones(self, home: Path) -> None:
        roster = home / "profiles.toml"
        add_profile("adam", 111, operator=True, roster_path=roster)
        with pytest.raises(ProfileConfigError, match="Known: adam"):
            resolve_cli_profile("nobody", roster_path=roster, require_existing=False)

    def test_missing_database_names_the_init_command(self, home: Path) -> None:
        roster = home / "profiles.toml"
        add_profile("adam", 111, operator=True, roster_path=roster)
        with pytest.raises(ProfileConfigError, match="init --profile adam"):
            resolve_cli_profile(None, roster_path=roster)


class TestContextStatus:
    """Templates count as unwritten, because placeholder prose is not intent."""

    def test_fresh_profile_is_all_templates(self, home: Path) -> None:
        profile = add_profile(
            "adam", 111, operator=True, roster_path=home / "profiles.toml"
        )
        assert {status for _, status in context_status(profile)} == {"template"}

    def test_edited_file_counts_as_written(self, home: Path) -> None:
        profile = add_profile(
            "adam", 111, operator=True, roster_path=home / "profiles.toml"
        )
        profile.context_path("strategy.md").write_text(
            "# Strategy\n\nBuy good things.\n"
        )
        statuses = dict((file.name, status) for file, status in context_status(profile))
        assert statuses["strategy.md"] == "written"
        assert statuses["investor.md"] == "template"

    def test_deleted_file_is_missing(self, home: Path) -> None:
        profile = add_profile(
            "adam", 111, operator=True, roster_path=home / "profiles.toml"
        )
        profile.context_path("log.md").unlink()
        statuses = dict((file.name, status) for file, status in context_status(profile))
        assert statuses["log.md"] == "missing"

    def test_absent_file_is_not_a_stub(self, tmp_path: Path) -> None:
        assert is_stub(tmp_path / "nope.md") is False
