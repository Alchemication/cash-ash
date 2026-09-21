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
    "chat": "Answers questions about the portfolio, with tools, in Telegram",
}
"""Every routable stage, with what it is for.

Ordered as the weekly run executes them, which is also roughly the order of
increasing consequence: a bad triage wastes a little money, a bad decision
stage produces a recommendation. ``chat`` sits outside that run — it is asked
for on demand, many times a week, which is exactly why it defaults cheap like
everything else.
"""

FEATURES: tuple[str, ...] = tuple(FEATURE_PURPOSE)

UNCALLED_FEATURES: frozenset[str] = frozenset({"synthesis"})
"""Routable stages no code path invokes yet.

``synthesis`` reconciles several analysts, and the pipeline runs exactly one,
so nothing calls it: in the week of 2026-09-20 it was routed, priced and
displayed beside stages that ran, having made no call at all. Multiple
analysts are deliberately deferred until the single-analyst workflow has
measured weaknesses (`docs/roadmap.md`), so the route stays configurable and
is labelled instead of being presented as live.

Remove a name from here when a stage starts calling it.
"""

MODEL_TIERS: dict[str, str] = {
    FLASH_MODEL: "flash",
    FAST_MODEL: "fast",
    PRO_MODEL: "pro",
}
"""Tier shown beside a model, so a change is visibly a change.

All three reason before answering, which is why every route declares a
reasoning effort. ``fast`` sits on a second provider and is the fallback for
that reason alone — it is redundancy against an outage, not a cheaper way to
do the analysis.
"""

_DEFAULT_TEMPERATURE: dict[str, float] = {}
"""No stage sets a temperature; every route uses the provider's own default.

Setting one was actively harmful. A reasoning model may refuse any temperature
but 1 while it is thinking, and the fast fallback does exactly that: every
attempt to reach it died instantly on ``temperature=0.0``, so the cross-
provider redundancy the routing is built around had never once worked. The
first outage that needed it, on 2026-09-20, found nothing there.

The reproducibility it was supposed to buy was not real either. A reasoning
model is not deterministic at temperature 0 — expert routing and batching move
the output anyway — so a rerun could never have told sampling noise apart from
judgement on that basis alone. Stability across reruns is measured by the
evals, not assumed from a parameter.

Kept as a map rather than deleted because an override may still set one per
profile, and a future provider may need a specific value.
"""

_DEFAULT_REASONING_EFFORT: dict[str, str] = {
    # The two stages whose output is a judgement the owner acts on. Depth is
    # the thing being bought here, and these are also the only stages where a
    # shallow answer is expensive rather than merely worse.
    "analyst": "high",
    "decision": "high",
    "synthesis": "high",
    # Ranking, question-writing and prose. Cheap by default, as the routing
    # is throughout: these are comparative or clerical, not analytical.
    "triage": "low",
    "plan": "low",
    "explain": "low",
    "chat": "low",
}
"""How hard each stage is asked to think, stated rather than left to chance.

Unset, this is whatever the provider happens to default to, on the single
largest lever over cost, latency and quality in the system — the one knob this
project documents everywhere else and had never declared here.

High costs more and is slower, which is the trade being made deliberately on
two stages and declined on the rest.
"""

REASONING_EFFORTS: tuple[str, ...] = ("none", "low", "medium", "high")
"""Accepted reasoning-effort levels, lowest first."""


@dataclass(frozen=True)
class ModelRoute:
    """The effective routing for one feature."""

    feature: str
    model: str
    fallback: str | None
    temperature: float | None
    reasoning_effort: str | None
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
    a whole weekly run with it.

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
        reasoning_effort=_DEFAULT_REASONING_EFFORT.get(feature),
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
    effort = stored.get("reasoning_effort", route.reasoning_effort)
    return ModelRoute(
        feature=feature,
        model=model if isinstance(model, str) and model else route.model,
        fallback=stored.get("fallback", route.fallback),
        temperature=temperature if isinstance(temperature, int | float) else None,
        reasoning_effort=effort if effort in REASONING_EFFORTS else None,
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
    reasoning_effort: str | None = None,
    path: Path | None = None,
) -> ModelRoute:
    """Persist a routing override for one feature.

    Args:
        feature: Feature to change.
        model: Model id, or None to leave it as is.
        temperature: Sampling temperature, or None to leave it as is.
        reasoning_effort: Reasoning level, or None to leave it as is.
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
    if reasoning_effort is not None:
        if reasoning_effort not in REASONING_EFFORTS:
            levels = ", ".join(REASONING_EFFORTS)
            raise ValueError(
                f"Unknown reasoning effort {reasoning_effort!r}. Levels: {levels}."
            )
        entry["reasoning_effort"] = reasoning_effort
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
