"""Which model each part of the system calls, and how to change it.

Routing is per feature so that one expensive stage does not drag the whole
pipeline's cost with it, and so a model change is attributable to a stage when
its output later turns out better or worse.

Every feature defaults to the flash tier. The strong tier is opt-in, per
feature, and its cost shows up in ``main.py models`` — the point is to make an
upgrade a measured decision rather than an assumption that a bigger model must
be better at a task nobody has evaluated.

Preferences persist per profile, so two people can route differently without
one editing the other's environment.

Public API:
    FEATURES        -- every routable stage
    ModelRoute      -- the effective model, fallback and sampling for a feature
    resolve_route   -- the route in force for a feature
    load_prefs      -- stored overrides
    set_route       -- persist an override
    clear_route     -- drop an override, returning the feature to its default
    parse_overrides -- read one-run routing from a command-line argument

Example:
    from model_prefs import resolve_route

    route = resolve_route("triage")
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from config import FAST_MODEL, FLASH_MODEL, PRO_MODEL

logger = logging.getLogger(__name__)

PREFS_VERSION = 1

FEATURE_PURPOSE: dict[str, str] = {
    "triage": "One call ranking every holding by what deserves attention",
    "plan": "Turns a thesis into this week's research questions",
    "analyst": "One independent read of the evidence for a security",
    "synthesis": "Reconciles the analysts into a thesis update",
    "decision": "Applies portfolio rules to produce recommendations",
    "explain": "Expands jargon into plain language on request",
}
"""Every routable stage, with what it is for.

Ordered as the weekly run executes them, which is also roughly the order of
increasing consequence: a bad triage wastes a little money, a bad decision
stage produces a recommendation.
"""

FEATURES: tuple[str, ...] = tuple(FEATURE_PURPOSE)

MODEL_TIERS: dict[str, str] = {
    FLASH_MODEL: "flash",
    FAST_MODEL: "fast",
    PRO_MODEL: "pro",
}
"""Tier shown beside a model, so a change is visibly a change.

``flash`` and ``pro`` reason before answering; ``fast`` does not. The
distinction matters more than the price: on an analytical stage the reasoning
is the output being bought, so a model that skips it is not a cheaper way to do
the same work.
"""

_DEFAULT_TEMPERATURE: dict[str, float] = {
    # Analysts read the same frozen evidence; the useful disagreement between
    # them should come from different models, not from sampling noise, or it
    # cannot be distinguished from randomness on a rerun.
    "analyst": 0.0,
    "synthesis": 0.0,
    "decision": 0.0,
    "triage": 0.0,
    "plan": 0.2,
    "explain": 0.3,
}


@dataclass(frozen=True)
class ModelRoute:
    """The effective routing for one feature."""

    feature: str
    model: str
    fallback: str | None
    temperature: float | None
    source: str
    """``default`` or ``override``, so it is obvious what has been changed."""

    @property
    def tier(self) -> str:
        """Capability tier of the routed model."""
        return MODEL_TIERS.get(self.model, "custom")


def default_route(feature: str) -> ModelRoute:
    """Return the built-in route for a feature.

    The fallback is the fast tier, which sits on a different provider. That is
    the point: a fallback within one provider covers a fault specific to one
    model but not the provider going down, which is the outage that would take
    a whole weekly run with it. It also emits no reasoning, so it cannot fail
    the way the primary can — by spending an entire budget thinking and
    returning nothing.

    Analytical stages all route to the flash tier. The fast model is here for
    resilience, not because it is a cheaper way to do the same analysis.

    Args:
        feature: Feature name.

    Returns:
        The default route.
    """
    return ModelRoute(
        feature=feature,
        model=FLASH_MODEL,
        fallback=FAST_MODEL,
        temperature=_DEFAULT_TEMPERATURE.get(feature),
        source="default",
    )


def _prefs_path() -> Path | None:
    """Return the active profile's preferences file, or None if unresolvable."""
    from profiles import ProfileConfigError, resolve_cli_profile

    try:
        profile, _ = resolve_cli_profile(None, db=None, require_existing=False)
    except ProfileConfigError:
        return None
    return None if profile is None else profile.root / "model_prefs.json"


def load_prefs(path: Path | None = None) -> dict[str, dict]:
    """Return stored routing overrides keyed by feature.

    A malformed file is reported and ignored rather than raised: routing has a
    working default, and refusing to run a weekly report because a preferences
    file was hand-edited badly is the wrong trade.

    Args:
        path: Preferences file, or None to use the active profile's.

    Returns:
        Overrides by feature name; empty when there are none.
    """
    resolved = path or _prefs_path()
    if resolved is None or not resolved.exists():
        return {}
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring unreadable model preferences %s: %s", resolved, exc)
        return {}
    routes = raw.get("routes")
    return routes if isinstance(routes, dict) else {}


def resolve_route(feature: str, path: Path | None = None) -> ModelRoute:
    """Return the route in force for a feature, override applied.

    Args:
        feature: Feature name.
        path: Preferences file, or None to use the active profile's.

    Returns:
        The effective route.

    Raises:
        ValueError: If the feature is not routable.
    """
    if feature not in FEATURE_PURPOSE:
        known = ", ".join(FEATURES)
        raise ValueError(f"Unknown feature {feature!r}. Routable features: {known}.")

    route = default_route(feature)
    stored = load_prefs(path).get(feature)
    if not isinstance(stored, dict):
        return route

    model = stored.get("model")
    temperature = stored.get("temperature", route.temperature)
    return ModelRoute(
        feature=feature,
        model=model if isinstance(model, str) and model else route.model,
        fallback=stored.get("fallback", route.fallback),
        temperature=temperature if isinstance(temperature, int | float) else None,
        source="override",
    )


def _write(routes: dict[str, dict], path: Path) -> None:
    """Persist the override map."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": PREFS_VERSION, "routes": routes}, indent=2) + "\n",
        encoding="utf-8",
    )


def set_route(
    feature: str,
    *,
    model: str | None = None,
    temperature: float | None = None,
    path: Path | None = None,
) -> ModelRoute:
    """Persist a routing override for one feature.

    Args:
        feature: Feature to change.
        model: Model id, or None to leave it as is.
        temperature: Sampling temperature, or None to leave it as is.
        path: Preferences file, or None to use the active profile's.

    Returns:
        The route now in force.

    Raises:
        ValueError: If the feature is unknown or no preferences file resolves.
    """
    if feature not in FEATURE_PURPOSE:
        known = ", ".join(FEATURES)
        raise ValueError(f"Unknown feature {feature!r}. Routable features: {known}.")

    resolved = path or _prefs_path()
    if resolved is None:
        raise ValueError(
            "No profile to store model preferences for. Create one with "
            "'main.py profile add NAME --telegram-id ID --operator'."
        )

    routes = dict(load_prefs(resolved))
    entry = dict(routes.get(feature, {}))
    if model is not None:
        entry["model"] = model
    if temperature is not None:
        entry["temperature"] = temperature
    routes[feature] = entry
    _write(routes, resolved)
    return resolve_route(feature, resolved)


def clear_route(feature: str, path: Path | None = None) -> ModelRoute:
    """Drop a feature's override, returning it to the built-in default.

    Args:
        feature: Feature to reset, or ``all`` for every feature.
        path: Preferences file, or None to use the active profile's.

    Returns:
        The default route for *feature*, or for ``triage`` when resetting all.

    Raises:
        ValueError: If the feature is unknown or no preferences file resolves.
    """
    resolved = path or _prefs_path()
    if resolved is None:
        raise ValueError("No profile to store model preferences for.")

    if feature == "all":
        _write({}, resolved)
        return default_route(FEATURES[0])

    if feature not in FEATURE_PURPOSE:
        known = ", ".join((*FEATURES, "all"))
        raise ValueError(f"Unknown feature {feature!r}. Routable features: {known}.")

    routes = dict(load_prefs(resolved))
    routes.pop(feature, None)
    _write(routes, resolved)
    return default_route(feature)


def parse_overrides(value: str) -> dict[str, str]:
    """Parse ``feature=model`` pairs into a mapping for a single run.

    Separate from :func:`set_route` because these are never written down. A
    comparison run on a different model is an experiment, and an experiment that
    quietly persists is one the next run inherits without knowing it did.

    Args:
        value: Comma-separated ``feature=model`` pairs, e.g.
            ``analyst=zai/glm-4.7``.

    Returns:
        Model by feature.

    Raises:
        ValueError: If a pair is malformed or names an unroutable feature.
    """
    overrides: dict[str, str] = {}
    for pair in value.split(","):
        entry = pair.strip()
        if not entry:
            continue
        feature, separator, model = entry.partition("=")
        feature, model = feature.strip(), model.strip()
        if not separator or not feature or not model:
            raise ValueError(
                f"Model override {entry!r} is not 'feature=model'. Example: "
                f"--model analyst={PRO_MODEL}."
            )
        if feature not in FEATURE_PURPOSE:
            known = ", ".join(FEATURES)
            raise ValueError(
                f"Unknown feature {feature!r} in --model. Routable features: {known}."
            )
        overrides[feature] = model
    if not overrides:
        raise ValueError("--model needs at least one 'feature=model' pair.")
    return overrides
