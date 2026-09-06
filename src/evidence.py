"""Where the facts an analysis reasons over come from.

Two kinds of claim reach an analysis and they are not interchangeable. A
``sourced`` claim is anchored to something published, with a URL and a date. A
``background`` claim is the model's own knowledge — how an industry works, what
happened years ago, what a pattern usually implies — which is genuinely useful
and is allowed, provided it never masquerades as a current fact.

The split matters most where models are weakest: recent events. A model's
recollection of what a company reported last month is confident and frequently
wrong, so anything time-sensitive has to carry a source.

Sources sit behind an abstraction for the same reason market data does. The
free option available today is a news aggregator, which supplies provenance but
not quality — a headline is a source, not proof that something is true.

Public API:
    EvidenceItem       -- one retrievable, dated, attributed item
    EvidenceSource     -- the interface sources implement
    YFinanceNewsSource -- free, no key, aggregated financial news
    get_evidence_source -- construct the configured source

Example:
    from evidence import get_evidence_source

    items = get_evidence_source().fetch("AMD", limit=8)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from config import EVIDENCE_ITEMS_PER_SECURITY, EVIDENCE_SOURCE

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvidenceItem:
    """One published item an analysis may cite.

    Attributes:
        title: Headline or document title.
        url: Where it can be read. Required — an item without one cannot be
            checked, and an unverifiable citation is worse than none.
        published: ISO date. Required, because the age of a claim is often the
            whole question.
        publisher: Who published it, so the reader can weigh it.
        summary: Short excerpt, when the source provides one.
    """

    title: str
    url: str
    published: str
    publisher: str | None = None
    summary: str | None = None

    def render(self) -> str:
        """Format this item for a prompt, provenance first."""
        head = f"[{self.published}] {self.title}"
        if self.publisher:
            head += f" — {self.publisher}"
        body = f"\n  {self.summary}" if self.summary else ""
        return f"{head}\n  {self.url}{body}"


class EvidenceSource(Protocol):
    """What the research pipeline needs from a source of facts."""

    name: str

    def fetch(self, symbol: str, *, limit: int) -> list[EvidenceItem]:
        """Return recent items for one symbol, newest first."""
        ...


class YFinanceNewsSource:
    """Free financial news via ``yfinance``. No key required.

    Supplies provenance — every item has a URL, a date and a publisher — but
    not quality. It aggregates retail-facing commentary rather than filings or
    transcripts, so an item establishes that something was said, not that it is
    true. Prompts downstream have to treat it that way.
    """

    name = "yfinance-news"

    @staticmethod
    def _text(value: Any) -> str | None:
        """Pull a display string out of whatever shape the field arrives in."""
        if isinstance(value, dict):
            for key in ("displayName", "name", "url"):
                found = value.get(key)
                if isinstance(found, str) and found.strip():
                    return found.strip()
            return None
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def fetch(
        self, symbol: str, *, limit: int = EVIDENCE_ITEMS_PER_SECURITY
    ) -> list[EvidenceItem]:
        """Return recent news for one symbol.

        Items missing a URL or a date are dropped rather than kept with the
        gap filled in: an item that cannot be dated cannot be judged for
        relevance, and one that cannot be reached cannot be checked.

        Args:
            symbol: Provider symbol.
            limit: Maximum items to return.

        Returns:
            Items newest first, possibly empty.
        """
        import warnings

        import yfinance

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                raw = yfinance.Ticker(symbol).news or []
        except Exception as exc:  # noqa: BLE001 - provider faults are opaque
            logger.warning("News lookup failed for %s: %s", symbol, exc)
            return []

        items: list[EvidenceItem] = []
        for entry in raw:
            content = entry.get("content", entry) if isinstance(entry, dict) else {}
            if not isinstance(content, dict):
                continue
            title = self._text(content.get("title"))
            url = self._text(content.get("canonicalUrl")) or self._text(
                content.get("clickThroughUrl")
            )
            published = self._text(content.get("pubDate")) or self._text(
                content.get("displayTime")
            )
            if not (title and url and published):
                logger.debug(
                    "Dropping news item for %s with missing provenance", symbol
                )
                continue
            items.append(
                EvidenceItem(
                    title=title,
                    url=url,
                    published=published[:10],
                    publisher=self._text(content.get("provider")),
                    summary=self._text(content.get("summary")),
                )
            )

        items.sort(key=lambda item: item.published, reverse=True)
        return items[:limit]


_SOURCES: dict[str, type] = {"yfinance-news": YFinanceNewsSource}


def get_evidence_source(name: str = EVIDENCE_SOURCE) -> EvidenceSource:
    """Construct the configured evidence source.

    Args:
        name: Source name.

    Returns:
        A ready source.

    Raises:
        ValueError: If no source is registered under that name.
    """
    try:
        return _SOURCES[name]()
    except KeyError as exc:
        known = ", ".join(sorted(_SOURCES))
        raise ValueError(
            f"Unknown evidence source {name!r}. Available: {known}."
        ) from exc
