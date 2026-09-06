"""Tests for per-feature model routing and its persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config import FLASH_MODEL, PRO_MODEL
from model_prefs import (
    FEATURE_PURPOSE,
    FEATURES,
    clear_route,
    default_route,
    load_prefs,
    resolve_route,
    set_route,
)


@pytest.fixture
def prefs(tmp_path: Path) -> Path:
    """An isolated preferences file."""
    return tmp_path / "model_prefs.json"


class TestDefaults:
    """Every feature routes cheap until someone decides otherwise."""

    def test_every_feature_defaults_to_flash(self, prefs: Path) -> None:
        # The cost argument is only real if it is the default, not the advice.
        for feature in FEATURES:
            assert resolve_route(feature, prefs).model == FLASH_MODEL

    def test_default_fallback_is_the_pro_tier(self) -> None:
        assert default_route("triage").fallback == PRO_MODEL

    def test_every_feature_is_documented(self) -> None:
        for feature in FEATURES:
            assert FEATURE_PURPOSE[feature].strip()

    def test_analysts_are_deterministic_by_default(self, prefs: Path) -> None:
        # Disagreement between analysts should come from different models, not
        # from sampling noise, or a rerun cannot tell the two apart.
        assert resolve_route("analyst", prefs).temperature == 0.0

    def test_unknown_feature_is_rejected(self, prefs: Path) -> None:
        with pytest.raises(ValueError, match="Unknown feature"):
            resolve_route("astrology", prefs)

    def test_tier_is_reported(self, prefs: Path) -> None:
        assert resolve_route("triage", prefs).tier == "flash"


class TestOverrides:
    """Routing changes persist and are visibly not the default."""

    def test_set_and_resolve(self, prefs: Path) -> None:
        set_route("synthesis", model=PRO_MODEL, path=prefs)
        route = resolve_route("synthesis", prefs)
        assert route.model == PRO_MODEL
        assert route.tier == "pro"
        assert route.source == "override"

    def test_override_is_scoped_to_one_feature(self, prefs: Path) -> None:
        set_route("synthesis", model=PRO_MODEL, path=prefs)
        assert resolve_route("triage", prefs).model == FLASH_MODEL

    def test_temperature_override(self, prefs: Path) -> None:
        set_route("plan", temperature=0.9, path=prefs)
        assert resolve_route("plan", prefs).temperature == 0.9

    def test_setting_temperature_keeps_the_model(self, prefs: Path) -> None:
        set_route("plan", model=PRO_MODEL, path=prefs)
        set_route("plan", temperature=0.5, path=prefs)
        route = resolve_route("plan", prefs)
        assert route.model == PRO_MODEL
        assert route.temperature == 0.5

    def test_file_records_a_version(self, prefs: Path) -> None:
        set_route("triage", model=PRO_MODEL, path=prefs)
        assert json.loads(prefs.read_text())["version"] >= 1

    def test_reset_one_feature(self, prefs: Path) -> None:
        set_route("synthesis", model=PRO_MODEL, path=prefs)
        clear_route("synthesis", prefs)
        route = resolve_route("synthesis", prefs)
        assert route.model == FLASH_MODEL
        assert route.source == "default"

    def test_reset_all(self, prefs: Path) -> None:
        set_route("synthesis", model=PRO_MODEL, path=prefs)
        set_route("decision", model=PRO_MODEL, path=prefs)
        clear_route("all", prefs)
        assert load_prefs(prefs) == {}

    def test_unknown_feature_cannot_be_set(self, prefs: Path) -> None:
        with pytest.raises(ValueError, match="Unknown feature"):
            set_route("astrology", model=PRO_MODEL, path=prefs)

    def test_unknown_feature_cannot_be_reset(self, prefs: Path) -> None:
        with pytest.raises(ValueError, match="Unknown feature"):
            clear_route("astrology", prefs)


class TestRobustness:
    """A hand-edited file must not take the weekly run down."""

    def test_absent_file_yields_no_overrides(self, prefs: Path) -> None:
        assert load_prefs(prefs) == {}

    def test_malformed_json_is_ignored_not_raised(self, prefs: Path) -> None:
        # Routing has a working default; refusing to run because a preferences
        # file was hand-edited badly is the wrong trade.
        prefs.write_text("{ not json at all")
        assert load_prefs(prefs) == {}
        assert resolve_route("triage", prefs).model == FLASH_MODEL

    def test_unexpected_shape_is_ignored(self, prefs: Path) -> None:
        prefs.write_text(json.dumps({"routes": "not a mapping"}))
        assert load_prefs(prefs) == {}

    def test_partial_entry_falls_back_to_defaults(self, prefs: Path) -> None:
        prefs.write_text(json.dumps({"routes": {"triage": {"temperature": 0.4}}}))
        route = resolve_route("triage", prefs)
        assert route.model == FLASH_MODEL
        assert route.temperature == 0.4

    def test_empty_model_string_is_ignored(self, prefs: Path) -> None:
        prefs.write_text(json.dumps({"routes": {"triage": {"model": ""}}}))
        assert resolve_route("triage", prefs).model == FLASH_MODEL
