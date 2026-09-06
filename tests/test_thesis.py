"""Tests for thesis versioning, JSON extraction and bootstrapping."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import research as research_module
from llm import LLMResult
from models import Thesis
from research import bootstrap_theses, extract_json
from seed import load_snapshot, seed_database
from store_research import active_thesis, active_theses, save_thesis, thesis_history
from tests.test_seed import FIXTURE


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    """A database seeded from the synthetic snapshot fixture."""
    seed_database(conn, load_snapshot(FIXTURE))
    return conn


class TestExtractJson:
    """Reasoning models wrap their answers however they like."""

    def test_bare_object(self) -> None:
        assert extract_json('{"summary": "x"}') == {"summary": "x"}

    def test_fenced_block(self) -> None:
        assert extract_json('```json\n{"summary": "x"}\n```') == {"summary": "x"}

    def test_unlabelled_fence(self) -> None:
        assert extract_json('```\n{"summary": "x"}\n```') == {"summary": "x"}

    def test_prose_before_and_after(self) -> None:
        # Failing a whole run over a stray sentence would be a poor trade.
        text = 'Here is the thesis:\n{"summary": "x"}\nHope that helps!'
        assert extract_json(text) == {"summary": "x"}

    def test_nested_objects_are_balanced(self) -> None:
        text = '{"a": {"b": 1}, "c": [2, 3]}'
        assert extract_json(text) == {"a": {"b": 1}, "c": [2, 3]}

    def test_no_json_raises(self) -> None:
        with pytest.raises(ValueError, match="No JSON object"):
            extract_json("I would rather not.")

    def test_malformed_json_raises(self) -> None:
        with pytest.raises(ValueError, match="Malformed JSON"):
            extract_json('{"summary": }')

    def test_unterminated_object_raises(self) -> None:
        with pytest.raises(ValueError, match="Unterminated"):
            extract_json('{"summary": "x"')

    def test_array_is_not_accepted(self) -> None:
        with pytest.raises(ValueError, match="No JSON object"):
            extract_json("[1, 2, 3]")


class TestThesisVersioning:
    """A thesis is never edited; a revision is a new version."""

    def _thesis(self, security_id: int, summary: str, **kw) -> Thesis:
        return Thesis(security_id=security_id, summary=summary, source="user", **kw)

    def test_first_version_is_one(self, seeded: sqlite3.Connection) -> None:
        stored = save_thesis(seeded, self._thesis(1, "because"))
        assert stored.version == 1
        assert stored.status == "active"

    def test_second_version_supersedes_the_first(
        self, seeded: sqlite3.Connection
    ) -> None:
        first = save_thesis(seeded, self._thesis(1, "because"))
        second = save_thesis(seeded, self._thesis(1, "revised"))
        assert second.version == 2
        assert second.supersedes_id == first.id
        assert active_thesis(seeded, security_id=1).summary == "revised"

    def test_old_versions_survive(self, seeded: sqlite3.Connection) -> None:
        # The record of what was believed at the time is why the table exists.
        for summary in ("first", "second", "third"):
            save_thesis(seeded, self._thesis(1, summary))
        history = thesis_history(seeded, security_id=1)
        assert [t.summary for t in history] == ["third", "second", "first"]
        assert [t.status for t in history] == ["active", "superseded", "superseded"]

    def test_only_one_active_version(self, seeded: sqlite3.Connection) -> None:
        for summary in ("a", "b", "c"):
            save_thesis(seeded, self._thesis(1, summary))
        row = seeded.execute(
            "SELECT COUNT(*) AS n FROM thesis WHERE security_id = 1 AND status = 'active'"
        ).fetchone()
        assert row["n"] == 1

    def test_versions_are_per_security(self, seeded: sqlite3.Connection) -> None:
        save_thesis(seeded, self._thesis(1, "a"))
        save_thesis(seeded, self._thesis(1, "b"))
        assert save_thesis(seeded, self._thesis(2, "first for two")).version == 1

    def test_lists_round_trip(self, seeded: sqlite3.Connection) -> None:
        stored = save_thesis(
            seeded,
            self._thesis(
                1,
                "x",
                key_assumptions=("a", "b"),
                open_questions=("q",),
                what_would_break_it=("boom",),
            ),
        )
        loaded = active_thesis(seeded, security_id=1)
        assert loaded.key_assumptions == ("a", "b")
        assert loaded.what_would_break_it == ("boom",)
        assert stored.what_would_break_it == loaded.what_would_break_it

    def test_missing_thesis_is_none(self, seeded: sqlite3.Connection) -> None:
        assert active_thesis(seeded, security_id=99) is None

    def test_active_theses_maps_by_security(self, seeded: sqlite3.Connection) -> None:
        save_thesis(seeded, self._thesis(1, "one"))
        save_thesis(seeded, self._thesis(2, "two"))
        assert set(active_theses(seeded)) == {1, 2}

    def test_invalid_conviction_is_rejected(self, seeded: sqlite3.Connection) -> None:
        # Ordinal labels only — a percentage would launder a guess.
        with pytest.raises(sqlite3.IntegrityError):
            save_thesis(seeded, self._thesis(1, "x", conviction="91%"))

    def test_invalid_source_is_rejected(self, seeded: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            save_thesis(seeded, Thesis(security_id=1, summary="x", source="vibes"))


class TestFalsifiability:
    """A thesis that cannot be broken cannot be tracked."""

    def test_with_breaking_conditions(self) -> None:
        thesis = Thesis(
            security_id=1, summary="x", source="user", what_would_break_it=("y",)
        )
        assert thesis.is_falsifiable is True

    def test_without_breaking_conditions(self) -> None:
        assert Thesis(security_id=1, summary="x", source="user").is_falsifiable is False


class _FakeProfile:
    """A profile whose context files live in a temp directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "context").mkdir(parents=True, exist_ok=True)

    @property
    def context(self) -> Path:
        return self.root / "context"

    def context_path(self, name: str) -> Path:
        return self.context / name


@pytest.fixture
def profile(tmp_path: Path) -> _FakeProfile:
    """A profile with a written log."""
    fake = _FakeProfile(tmp_path)
    fake.context_path("log.md").write_text(
        "# Log\n\n**AAA** — I like the product.\n"
        "**BBB** — No idea, it was in a list.\n"
        "**CCC** — A friend mentioned it.\n",
        encoding="utf-8",
    )
    return fake


def _payload(**kw) -> str:
    import json

    base = {
        "summary": "restated reason",
        "rationale": "fuller version",
        "conviction": "weak",
        "key_assumptions": ["stays true"],
        "open_questions": ["nothing about price"],
        "what_would_break_it": ["it stops being true"],
    }
    return json.dumps({**base, **kw})


class TestBootstrap:
    """Restating the owner's reasons, not researching them."""

    @pytest.fixture
    def fake_llm(self, monkeypatch: pytest.MonkeyPatch):
        """Replace call_llm with a scripted stand-in."""

        def install(*texts: str):
            queue = list(texts)
            seen: list[str] = []

            def fake(conn, *, messages, **kwargs):  # type: ignore[no-untyped-def]
                seen.append(messages[-1]["content"])
                text = queue.pop(0) if queue else _payload()
                return LLMResult(
                    text=text, model="fake", requested_model="fake", llm_call_id=1
                )

            monkeypatch.setattr(research_module, "call_llm", fake)
            return seen

        return install

    def test_creates_a_thesis_per_holding(
        self, seeded: sqlite3.Connection, profile, fake_llm
    ) -> None:
        fake_llm()
        results = bootstrap_theses(seeded, profile=profile)
        assert {ticker for ticker, _, _ in results} == {"AAA", "BBB", "CCC"}
        assert all(thesis is not None for _, thesis, _ in results)

    def test_marks_everything_unexamined(
        self, seeded: sqlite3.Connection, profile, fake_llm
    ) -> None:
        # Restating a reason examines nothing. Claiming otherwise would say the
        # position has been looked at when only the sentence has.
        fake_llm()
        bootstrap_theses(seeded, profile=profile)
        assert all(
            t.thesis_status == "unexamined" for t in active_theses(seeded).values()
        )

    def test_records_source_as_user(
        self, seeded: sqlite3.Connection, profile, fake_llm
    ) -> None:
        fake_llm()
        bootstrap_theses(seeded, profile=profile)
        assert all(t.source == "user" for t in active_theses(seeded).values())

    def test_passes_the_whole_log_for_context(
        self, seeded: sqlite3.Connection, profile, fake_llm
    ) -> None:
        # So the model can see two reasons are identical, or contradict.
        seen = fake_llm()
        bootstrap_theses(seeded, profile=profile, only="AAA")
        assert "BBB" in seen[0] and "CCC" in seen[0]

    def test_only_restricts_to_one_ticker(
        self, seeded: sqlite3.Connection, profile, fake_llm
    ) -> None:
        fake_llm()
        results = bootstrap_theses(seeded, profile=profile, only="aaa")
        assert [ticker for ticker, _, _ in results] == ["AAA"]

    def test_unknown_ticker_is_rejected(
        self, seeded: sqlite3.Connection, profile, fake_llm
    ) -> None:
        fake_llm()
        with pytest.raises(ValueError, match="No holding with ticker"):
            bootstrap_theses(seeded, profile=profile, only="ZZZ")

    def test_existing_thesis_is_kept_by_default(
        self, seeded: sqlite3.Connection, profile, fake_llm
    ) -> None:
        fake_llm()
        bootstrap_theses(seeded, profile=profile, only="AAA")
        results = bootstrap_theses(seeded, profile=profile, only="AAA")
        assert results[0][2] == "already has a thesis"
        assert active_thesis(seeded, security_id=1).version == 1

    def test_overwrite_creates_a_new_version(
        self, seeded: sqlite3.Connection, profile, fake_llm
    ) -> None:
        fake_llm()
        bootstrap_theses(seeded, profile=profile, only="AAA")
        bootstrap_theses(seeded, profile=profile, only="AAA", overwrite=True)
        assert active_thesis(seeded, security_id=1).version == 2

    def test_one_failure_does_not_stop_the_rest(
        self, seeded: sqlite3.Connection, profile, fake_llm
    ) -> None:
        fake_llm("not json at all", _payload(), _payload())
        results = bootstrap_theses(seeded, profile=profile)
        failed = [r for r in results if r[1] is None]
        assert len(failed) == 1
        assert len(active_theses(seeded)) == 2

    def test_empty_summary_is_refused(
        self, seeded: sqlite3.Connection, profile, fake_llm
    ) -> None:
        fake_llm(_payload(summary="   "))
        results = bootstrap_theses(seeded, profile=profile, only="AAA")
        assert results[0][1] is None
        assert "no summary" in results[0][2]

    def test_invalid_conviction_falls_back_to_unstated(
        self, seeded: sqlite3.Connection, profile, fake_llm
    ) -> None:
        # A model inventing "very high" must not reach a CHECK constraint.
        fake_llm(_payload(conviction="very high"))
        bootstrap_theses(seeded, profile=profile, only="AAA")
        assert active_thesis(seeded, security_id=1).conviction == "unstated"

    def test_missing_log_is_refused(
        self, seeded: sqlite3.Connection, tmp_path: Path, fake_llm
    ) -> None:
        fake_llm()
        empty = _FakeProfile(tmp_path / "empty")
        with pytest.raises(ValueError, match="No written log"):
            bootstrap_theses(seeded, profile=empty)

    def test_template_log_counts_as_missing(
        self, seeded: sqlite3.Connection, tmp_path: Path, fake_llm
    ) -> None:
        # Placeholder prose read as intent is worse than an absent file.
        fake_llm()
        stub = _FakeProfile(tmp_path / "stub")
        stub.context_path("log.md").write_text(
            "<!-- skarbie:template -->\n\n# Log\n\n> Replace this.\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="No written log"):
            bootstrap_theses(seeded, profile=stub)
