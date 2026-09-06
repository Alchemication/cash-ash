"""Curated events: the dates no market-data feed carries.

An earnings date is in every API. A keynote, an IPO lockup expiry, a quarterly
delivery report or a court date is in none of them, and those are frequently
the dates that actually move a holding. This module reads them from a TOML file
the user maintains beside their portfolio.

Later the research pipeline will propose entries here too, recorded as
``research`` rather than ``curated`` so triage can trust them less.

Public API:
    load_curated_events  -- parse a curated events file into Event objects
    EventsFileError      -- the file exists but cannot be used

Example:
    from events_file import load_curated_events

    events = load_curated_events(profile.events_file, security_ids)
"""

from __future__ import annotations

import logging
import tomllib
from datetime import date
from pathlib import Path

from models import Event

logger = logging.getLogger(__name__)

KNOWN_KINDS: frozenset[str] = frozenset(
    {
        "earnings",
        "ex_dividend",
        "dividend",
        "product",
        "conference",
        "lockup_expiry",
        "delivery_report",
        "regulatory",
        "index_change",
        "macro",
        "other",
    }
)
"""Event kinds triage understands.

Open rather than closed on purpose — an unrecognised kind is a warning, not a
failure, because the cost of rejecting a real date the user wrote down is
higher than the cost of one odd label.
"""


class EventsFileError(ValueError):
    """Raised when a curated events file cannot be parsed."""


def load_curated_events(
    path: Path, security_ids: dict[str, int]
) -> tuple[list[Event], list[str]]:
    """Parse a curated events file.

    Args:
        path: Events file to read. A missing file is not an error — most
            profiles will not have one until the user writes it.
        security_ids: Ticker to security id, for resolving each entry.

    Returns:
        ``(events, warnings)``. Warnings name entries that were skipped, so a
        typo surfaces rather than silently dropping a date.

    Raises:
        EventsFileError: If the file exists but is not readable TOML.
    """
    if not path.exists():
        return [], []

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise EventsFileError(f"Could not read events file {path}: {exc}") from exc

    events: list[Event] = []
    warnings: list[str] = []

    for index, row in enumerate(raw.get("event", []), start=1):
        missing = [key for key in ("date", "kind", "title") if key not in row]
        if missing:
            warnings.append(f"event {index}: missing {', '.join(missing)}")
            continue

        event_date = str(row["date"])
        try:
            date.fromisoformat(event_date)
        except ValueError:
            warnings.append(
                f"event {index} ({row['title']}): '{event_date}' is not a YYYY-MM-DD date"
            )
            continue

        ticker = row.get("ticker")
        security_id: int | None = None
        if ticker is not None:
            security_id = security_ids.get(str(ticker).upper())
            if security_id is None:
                warnings.append(
                    f"event {index} ({row['title']}): unknown ticker {ticker!r}"
                )
                continue

        kind = str(row["kind"])
        if kind not in KNOWN_KINDS:
            warnings.append(
                f"event {index} ({row['title']}): unfamiliar kind {kind!r}, kept anyway"
            )

        confidence = str(row.get("confidence", "confirmed"))
        if confidence not in {"confirmed", "estimated"}:
            warnings.append(
                f"event {index} ({row['title']}): confidence must be "
                f"'confirmed' or 'estimated', got {confidence!r}"
            )
            continue

        events.append(
            Event(
                event_date=event_date,
                kind=kind,
                title=str(row["title"]),
                source="curated",
                security_id=security_id,
                confidence=confidence,
                note=row.get("note"),
            )
        )

    return events, warnings
