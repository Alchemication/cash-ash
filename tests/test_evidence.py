"""Tests for evidence provenance: where claims come from and what that requires."""

from __future__ import annotations

import sqlite3

import pytest

from evidence import EvidenceItem, YFinanceNewsSource, get_evidence_source


def _news(**kw) -> dict:
    content = {
        "title": "AMD commits to Anthropic",
        "canonicalUrl": {"url": "https://example.com/a"},
        "pubDate": "2026-09-05T10:00:00Z",
        "provider": {"displayName": "Example Wire"},
        "summary": "A summary.",
    }
    content.update(kw)
    return {"id": "1", "content": content}


class TestEvidenceItem:
    """Provenance leads, because the date and source are the point."""

    def test_render_puts_date_and_publisher_first(self) -> None:
        item = EvidenceItem(
            title="A thing happened",
            url="https://example.com/a",
            published="2026-09-05",
            publisher="Example Wire",
            summary="Details.",
        )
        rendered = item.render()
        assert rendered.startswith("[2026-09-05] A thing happened — Example Wire")
        assert "https://example.com/a" in rendered

    def test_render_without_optional_fields(self) -> None:
        item = EvidenceItem("T", "https://e.com", "2026-09-05")
        assert "[2026-09-05] T" in item.render()


class TestYFinanceNewsSource:
    """Parsing, and refusing items that cannot be checked."""

    @pytest.fixture
    def patched(self, monkeypatch: pytest.MonkeyPatch):
        def install(items):
            import yfinance

            class FakeTicker:
                def __init__(self, symbol: str) -> None:
                    self.symbol = symbol

                @property
                def news(self):
                    if isinstance(items, Exception):
                        raise items
                    return items

            monkeypatch.setattr(yfinance, "Ticker", FakeTicker)

        return install

    def test_parses_a_full_item(self, patched) -> None:
        patched([_news()])
        (item,) = YFinanceNewsSource().fetch("AMD", limit=5)
        assert item.title == "AMD commits to Anthropic"
        assert item.url == "https://example.com/a"
        assert item.published == "2026-09-05"
        assert item.publisher == "Example Wire"

    def test_item_without_url_is_dropped(self, patched) -> None:
        # An unverifiable citation is worse than no citation.
        patched([_news(canonicalUrl=None, clickThroughUrl=None)])
        assert YFinanceNewsSource().fetch("AMD", limit=5) == []

    def test_item_without_date_is_dropped(self, patched) -> None:
        # A claim that cannot be dated cannot be judged for relevance.
        patched([_news(pubDate=None, displayTime=None)])
        assert YFinanceNewsSource().fetch("AMD", limit=5) == []

    def test_item_without_title_is_dropped(self, patched) -> None:
        patched([_news(title=None)])
        assert YFinanceNewsSource().fetch("AMD", limit=5) == []

    def test_newest_first(self, patched) -> None:
        patched(
            [
                _news(title="older", pubDate="2026-09-01T00:00:00Z"),
                _news(title="newer", pubDate="2026-09-05T00:00:00Z"),
            ]
        )
        items = YFinanceNewsSource().fetch("AMD", limit=5)
        assert [item.title for item in items] == ["newer", "older"]

    def test_limit_is_respected(self, patched) -> None:
        patched([_news(title=f"item {i}") for i in range(20)])
        assert len(YFinanceNewsSource().fetch("AMD", limit=3)) == 3

    def test_provider_failure_returns_empty(self, patched) -> None:
        # Missing news degrades an analysis; it must not fail the run.
        patched(ConnectionError("simulated outage"))
        assert YFinanceNewsSource().fetch("AMD", limit=5) == []

    def test_no_news_returns_empty(self, patched) -> None:
        patched([])
        assert YFinanceNewsSource().fetch("AMD", limit=5) == []

    def test_factory_rejects_unknown_source(self) -> None:
        with pytest.raises(ValueError, match="Unknown evidence source"):
            get_evidence_source("psychic-hotline")

    def test_factory_returns_the_default(self) -> None:
        assert get_evidence_source().name == "yfinance-news"


class TestEvidenceProvenanceConstraint:
    """The sourced/background split is enforced by the database, not a prompt."""

    @pytest.fixture
    def run_id(self, conn: sqlite3.Connection) -> int:
        from store_research import create_research_run

        return create_research_run(conn, run_date="2026-09-06", kind="deep")

    def _insert(self, conn: sqlite3.Connection, run_id: int, **kw) -> None:
        columns = ", ".join([*kw, "research_run_id", "created_at"])
        placeholders = ", ".join("?" * (len(kw) + 2))
        conn.execute(
            f"INSERT INTO evidence ({columns}) VALUES ({placeholders})",
            (*kw.values(), run_id, "now"),
        )

    def test_sourced_claim_with_url_and_date_is_accepted(
        self, conn: sqlite3.Connection, run_id: int
    ) -> None:
        self._insert(
            conn,
            run_id,
            claim="AMD committed $5bn",
            kind="sourced",
            source_url="https://example.com/a",
            published_date="2026-09-05",
        )

    def test_sourced_claim_without_url_is_rejected(
        self, conn: sqlite3.Connection, run_id: int
    ) -> None:
        # A model's recollection of recent events is where it is least reliable
        # and most confident, so "sourced" has to mean it.
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(
                conn,
                run_id,
                claim="AMD committed $5bn",
                kind="sourced",
                published_date="2026-09-05",
            )

    def test_sourced_claim_without_date_is_rejected(
        self, conn: sqlite3.Connection, run_id: int
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(
                conn,
                run_id,
                claim="AMD committed $5bn",
                kind="sourced",
                source_url="https://example.com/a",
            )

    def test_background_claim_needs_no_source(
        self, conn: sqlite3.Connection, run_id: int
    ) -> None:
        # Model knowledge is legitimate context — it just may not pose as a
        # current fact.
        self._insert(
            conn,
            run_id,
            claim="GPUs are used to train large models",
            kind="background",
        )

    def test_invented_kind_is_rejected(
        self, conn: sqlite3.Connection, run_id: int
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(conn, run_id, claim="x", kind="rumour")

    def test_kind_defaults_to_background(
        self, conn: sqlite3.Connection, run_id: int
    ) -> None:
        # The safe default: an unlabelled claim is not treated as sourced.
        self._insert(conn, run_id, claim="something general")
        row = conn.execute("SELECT kind FROM evidence").fetchone()
        assert row["kind"] == "background"
