"""Tests for curated events, calendar sync and the consensus time series."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cmd_sync import import_curated_events, sync_calendar
from events_file import EventsFileError, load_curated_events
from market_data import CalendarEntry
from models import ConsensusEstimate, Event
from seed import load_snapshot, seed_database
from store import consensus_history, load_events, save_consensus, save_events
from tests.test_market_data import FakeProvider
from tests.test_seed import FIXTURE

EXAMPLE = Path(__file__).resolve().parent.parent / "events.example.toml"


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    """A database seeded from the synthetic snapshot fixture."""
    seed_database(conn, load_snapshot(FIXTURE))
    return conn


class TestCuratedEventsFile:
    """Parsing the file of dates no feed carries."""

    IDS = {"AAA": 1, "BBB": 2}

    def test_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        # Most profiles will not have one until the user writes it.
        events, warnings = load_curated_events(tmp_path / "absent.toml", self.IDS)
        assert (events, warnings) == ([], [])

    def test_shipped_example_parses(self) -> None:
        events, warnings = load_curated_events(EXAMPLE, {"ACME": 1, "GLOBEX": 2})
        assert len(events) == 3
        assert warnings == []

    def test_reads_a_full_entry(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "e.toml",
            '[[event]]\nticker = "AAA"\ndate = "2026-10-15"\nkind = "product"\n'
            'title = "Launch"\nconfidence = "estimated"\nnote = "why"\n',
        )
        (event,), warnings = load_curated_events(path, self.IDS)
        assert event.security_id == 1
        assert event.kind == "product"
        assert event.confidence == "estimated"
        assert event.source == "curated"
        assert event.note == "why"
        assert warnings == []

    def test_macro_event_needs_no_ticker(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "e.toml",
            '[[event]]\ndate = "2026-12-10"\nkind = "macro"\ntitle = "Rate decision"\n',
        )
        (event,), warnings = load_curated_events(path, self.IDS)
        assert event.security_id is None
        assert warnings == []

    def test_confidence_defaults_to_confirmed(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "e.toml",
            '[[event]]\ndate = "2026-12-10"\nkind = "macro"\ntitle = "X"\n',
        )
        (event,), _ = load_curated_events(path, self.IDS)
        assert event.confidence == "confirmed"

    def test_bad_date_is_skipped_with_a_warning(self, tmp_path: Path) -> None:
        # Skipped rather than raised: one typo must not lose the other entries.
        path = _write(
            tmp_path / "e.toml",
            '[[event]]\ndate = "next tuesday"\nkind = "macro"\ntitle = "X"\n'
            '[[event]]\ndate = "2026-12-10"\nkind = "macro"\ntitle = "Y"\n',
        )
        events, warnings = load_curated_events(path, self.IDS)
        assert [event.title for event in events] == ["Y"]
        assert "not a YYYY-MM-DD date" in warnings[0]

    def test_unknown_ticker_is_skipped_with_a_warning(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "e.toml",
            '[[event]]\nticker = "NOPE"\ndate = "2026-12-10"\nkind = "macro"\ntitle = "X"\n',
        )
        events, warnings = load_curated_events(path, self.IDS)
        assert events == []
        assert "unknown ticker" in warnings[0]

    def test_ticker_is_case_insensitive(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "e.toml",
            '[[event]]\nticker = "aaa"\ndate = "2026-12-10"\nkind = "product"\ntitle = "X"\n',
        )
        (event,), _ = load_curated_events(path, self.IDS)
        assert event.security_id == 1

    def test_missing_fields_are_reported(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "e.toml", '[[event]]\ndate = "2026-12-10"\n')
        events, warnings = load_curated_events(path, self.IDS)
        assert events == []
        assert "missing kind, title" in warnings[0]

    def test_unfamiliar_kind_warns_but_is_kept(self, tmp_path: Path) -> None:
        # Rejecting a real date over an odd label costs more than keeping it.
        path = _write(
            tmp_path / "e.toml",
            '[[event]]\ndate = "2026-12-10"\nkind = "hackathon"\ntitle = "X"\n',
        )
        events, warnings = load_curated_events(path, self.IDS)
        assert len(events) == 1
        assert "unfamiliar kind" in warnings[0]

    def test_bad_confidence_is_skipped(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "e.toml",
            '[[event]]\ndate = "2026-12-10"\nkind = "macro"\ntitle = "X"\n'
            'confidence = "pretty sure"\n',
        )
        events, warnings = load_curated_events(path, self.IDS)
        assert events == []
        assert "confidence must be" in warnings[0]

    def test_unreadable_toml_raises(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "e.toml", "this is not [ valid toml")
        with pytest.raises(EventsFileError, match="Could not read"):
            load_curated_events(path, self.IDS)


class TestEventStorage:
    """Events insert idempotently and can be refreshed for the future only."""

    def _event(self, **kw) -> Event:
        base = {
            "event_date": "2026-10-29",
            "kind": "earnings",
            "title": "AAA earnings",
            "source": "feed",
            "security_id": 1,
        }
        return Event(**{**base, **kw})

    def test_round_trip(self, seeded: sqlite3.Connection) -> None:
        assert save_events(seeded, [self._event()]) == 1
        (row,) = load_events(seeded)
        assert row["kind"] == "earnings"
        assert row["ticker"] == "AAA"

    def test_duplicate_insert_is_ignored(self, seeded: sqlite3.Connection) -> None:
        save_events(seeded, [self._event()])
        assert save_events(seeded, [self._event()]) == 0
        assert len(load_events(seeded)) == 1

    def test_empty_write_is_a_no_op(self, seeded: sqlite3.Connection) -> None:
        assert save_events(seeded, []) == 0

    def test_window_filters_by_date(self, seeded: sqlite3.Connection) -> None:
        save_events(
            seeded,
            [
                self._event(event_date="2026-09-01", title="early"),
                self._event(event_date="2026-12-01", title="late"),
            ],
        )
        rows = load_events(seeded, start="2026-10-01")
        assert [row["title"] for row in rows] == ["late"]

    def test_macro_event_has_no_ticker(self, seeded: sqlite3.Connection) -> None:
        save_events(
            seeded,
            [
                Event(
                    event_date="2026-12-10",
                    kind="macro",
                    title="Rate decision",
                    source="curated",
                )
            ],
        )
        (row,) = load_events(seeded)
        assert row["ticker"] is None

    def test_invalid_source_is_rejected(self, seeded: sqlite3.Connection) -> None:
        # INSERT OR IGNORE swallowed every constraint failure, so bad data
        # vanished instead of raising. Uniqueness is skipped; CHECK is not.
        with pytest.raises(sqlite3.IntegrityError):
            save_events(seeded, [self._event(source="rumour")])

    def test_invalid_confidence_is_rejected(self, seeded: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            save_events(seeded, [self._event(confidence="pretty sure")])

    def test_macro_event_does_not_duplicate_on_resync(
        self, seeded: sqlite3.Connection
    ) -> None:
        # SQLite treats two NULLs as distinct in a unique index, so an event
        # with no security_id never collided with itself and gained a fresh
        # copy on every weekly sync.
        macro = Event(
            event_date="2026-12-10",
            kind="macro",
            title="Rate decision",
            source="curated",
        )
        for _ in range(3):
            save_events(seeded, [macro])
        assert len(load_events(seeded)) == 1

    def test_distinct_macro_events_still_coexist(
        self, seeded: sqlite3.Connection
    ) -> None:
        save_events(
            seeded,
            [
                Event(
                    event_date="2026-12-10",
                    kind="macro",
                    title="Rate decision",
                    source="curated",
                ),
                Event(
                    event_date="2026-12-10",
                    kind="macro",
                    title="Inflation print",
                    source="curated",
                ),
            ],
        )
        assert len(load_events(seeded)) == 2

    def test_curated_import_from_a_file(
        self, seeded: sqlite3.Connection, tmp_path: Path
    ) -> None:
        path = _write(
            tmp_path / "e.toml",
            '[[event]]\nticker = "AAA"\ndate = "2026-10-15"\nkind = "product"\n'
            'title = "Launch"\n',
        )
        added, warnings = import_curated_events(seeded, path=path)
        assert (added, warnings) == (1, [])


class TestCalendarSync:
    """Fetching known dates and consensus for held securities."""

    ENTRY = CalendarEntry(
        symbol="AAA",
        source="fake",
        earnings_dates=("2026-10-29",),
        ex_dividend_date="2026-10-01",
        eps_avg=1.98,
        eps_low=1.93,
        eps_high=2.07,
        revenue_avg=1.13e11,
    )

    def test_stores_events_and_estimates(self, seeded: sqlite3.Connection) -> None:
        provider = FakeProvider(calendar={"AAA": self.ENTRY})
        result = sync_calendar(seeded, provider=provider, today="2026-09-06")
        assert result["securities"] == 1
        assert result["estimates"] == 1
        kinds = {row["kind"] for row in load_events(seeded)}
        assert kinds == {"earnings", "ex_dividend"}

    def test_asks_using_the_feed_symbol(self, seeded: sqlite3.Connection) -> None:
        provider = FakeProvider()
        sync_calendar(seeded, provider=provider, today="2026-09-06")
        assert "BBB-X" in provider.calendar_asked_for
        assert "BBB" not in provider.calendar_asked_for

    def test_past_dates_are_not_stored_as_upcoming(
        self, seeded: sqlite3.Connection
    ) -> None:
        entry = CalendarEntry(
            symbol="AAA", source="fake", earnings_dates=("2026-01-01",)
        )
        provider = FakeProvider(calendar={"AAA": entry})
        sync_calendar(seeded, provider=provider, today="2026-09-06")
        assert load_events(seeded) == []

    def test_rescheduled_date_replaces_rather_than_duplicates(
        self, seeded: sqlite3.Connection
    ) -> None:
        # A company moving its reporting date must not leave triage two answers.
        provider = FakeProvider(calendar={"AAA": self.ENTRY})
        sync_calendar(seeded, provider=provider, today="2026-09-06")
        moved = CalendarEntry(
            symbol="AAA", source="fake", earnings_dates=("2026-11-05",)
        )
        sync_calendar(
            seeded, provider=FakeProvider(calendar={"AAA": moved}), today="2026-09-06"
        )
        earnings = [
            row["event_date"]
            for row in load_events(seeded)
            if row["kind"] == "earnings"
        ]
        assert earnings == ["2026-11-05"]

    def test_rerunning_unchanged_adds_nothing(self, seeded: sqlite3.Connection) -> None:
        provider = FakeProvider(calendar={"AAA": self.ENTRY})
        sync_calendar(seeded, provider=provider, today="2026-09-06")
        before = len(load_events(seeded))
        sync_calendar(seeded, provider=provider, today="2026-09-06")
        assert len(load_events(seeded)) == before

    def test_entry_without_estimates_records_none(
        self, seeded: sqlite3.Connection
    ) -> None:
        entry = CalendarEntry(
            symbol="AAA", source="fake", earnings_dates=("2026-10-29",)
        )
        provider = FakeProvider(calendar={"AAA": entry})
        result = sync_calendar(seeded, provider=provider, today="2026-09-06")
        assert result["estimates"] == 0

    def test_no_holdings_makes_no_request(self, conn: sqlite3.Connection) -> None:
        provider = FakeProvider()
        result = sync_calendar(conn, provider=provider, today="2026-09-06")
        assert result == {"events": 0, "estimates": 0, "securities": 0}
        assert provider.calendar_asked_for == []


class TestConsensusSeries:
    """The series exists so a revision is visible; it cannot be backfilled."""

    def _estimate(
        self, security_id: int, observed: str, eps: float
    ) -> ConsensusEstimate:
        return ConsensusEstimate(
            security_id=security_id,
            observed_date=observed,
            source="fake",
            eps_avg=eps,
        )

    def test_keeps_one_point_per_observation_date(
        self, seeded: sqlite3.Connection, security_id: int = 1
    ) -> None:
        save_consensus(
            seeded,
            [
                self._estimate(1, "2026-09-01", 1.90),
                self._estimate(1, "2026-09-08", 1.98),
            ],
        )
        rows = consensus_history(seeded, security_id=1)
        assert [row["observed_date"] for row in rows] == ["2026-09-08", "2026-09-01"]

    def test_resaving_the_same_day_overwrites(self, seeded: sqlite3.Connection) -> None:
        save_consensus(seeded, [self._estimate(1, "2026-09-08", 1.90)])
        save_consensus(seeded, [self._estimate(1, "2026-09-08", 2.10)])
        rows = consensus_history(seeded, security_id=1)
        assert len(rows) == 1
        assert rows[0]["eps_avg"] == pytest.approx(2.10)

    def test_revision_is_visible_across_observations(
        self, seeded: sqlite3.Connection
    ) -> None:
        # The whole point of the table: yesterday's consensus is unrecoverable
        # from the provider, so a cut is only detectable against what we stored.
        save_consensus(
            seeded,
            [
                self._estimate(1, "2026-09-01", 2.10),
                self._estimate(1, "2026-09-08", 1.90),
            ],
        )
        newest, previous = consensus_history(seeded, security_id=1)[:2]
        assert newest["eps_avg"] < previous["eps_avg"]

    def test_empty_write_is_a_no_op(self, seeded: sqlite3.Connection) -> None:
        assert save_consensus(seeded, []) == 0

    def test_history_is_empty_for_an_unseen_security(
        self, seeded: sqlite3.Connection
    ) -> None:
        assert consensus_history(seeded, security_id=999) == []
