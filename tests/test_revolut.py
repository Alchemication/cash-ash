"""Synthetic account statements: extraction, evidence archive and ledger safety."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from models import Account, Security, Trade
from portfolio import cash_eur, positions
from profiles import Profile
from revolut_reconcile import apply_seed_corrections, reconcile, seed_corrections
from revolut_statement import Statement, extract_pdf, parse_layout
from revolut_storage import ingest, private_path
from store import (
    ensure_account,
    insert_trade,
    latest_snapshot,
    load_trades,
    open_db,
    save_snapshot,
    upsert_security,
)

FIXTURE = Path(__file__).parent / "fixtures" / "revolut" / "account.txt"


def synthetic_pdf(path: Path, text: str) -> Path:
    """Render invented fixed-width tables into a real text-based PDF."""
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Courier"),
            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
        }
    )
    # Multiple pages exercise the actual layout extractor and page joining.
    lines = text.splitlines()
    for offset in range(0, len(lines), 24):
        page = writer.add_blank_page(width=1400, height=800)
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {NameObject("/F1"): writer._add_object(font)}
                )
            }
        )
        commands = [b"BT /F1 10 Tf 12 TL 20 770 Td"]
        for line in lines[offset : offset + 24]:
            escaped = (
                line.encode("cp1252")
                .replace(b"\\", b"\\\\")
                .replace(b"(", b"\\(")
                .replace(b")", b"\\)")
            )
            commands.append(b"(" + escaped + b") Tj T*")
        commands.append(b"ET")
        stream = DecodedStreamObject()
        stream.set_data(b"\n".join(commands))
        page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(path)
    return path


class TestExtraction:
    def test_real_pdf_layout(self, tmp_path: Path) -> None:
        statement = extract_pdf(
            synthetic_pdf(tmp_path / "synthetic.pdf", FIXTURE.read_text())
        )
        assert statement.generated_on == date(2026, 9, 12)
        assert statement.period_end == date(2026, 9, 30)
        assert statement.valuation_at is None
        usd, eur = statement.sections
        assert usd.holdings[0].isin == "US0000000001"
        assert usd.holdings[0].quantity == Decimal("2.00000000")
        assert usd.holdings[0].price == Decimal("20")
        assert usd.ending == {
            "positions": Decimal(40),
            "cash": Decimal(3),
            "total": Decimal(43),
        }
        assert len(usd.transactions) == 2
        assert usd.transactions[0].value == Decimal(1)
        assert "Buy" in usd.transactions[1].details
        assert eur.holdings == [] and eur.ending["total"] == 0
        assert "partial-period" in " ".join(statement.warnings)

    @pytest.mark.parametrize(
        "old,new",
        [
            ("US$40  93.02%\nCash", "US$400  93.02%\nCash"),
            ("2.00000000", "NaN"),
            ("US$20  US$40", "€20  US$40"),
            ("USD Transactions", "USD Activity"),
            ("Generated on the 12 Sep 2026", "Generated on the 12 Foo 2026"),
            ("US0000000001", "BROKEN"),
            ("TEST  Dividend  US$1", "TEST  Dividend  bad"),
            ("Positions Value  €0  €0", "Positions Value  €0"),
        ],
    )
    def test_malformed_refused(self, old: str, new: str) -> None:
        with pytest.raises(ValueError):
            parse_layout(FIXTURE.read_text().replace(old, new))

    def test_missing_holding_refused(self) -> None:
        text = "\n".join(
            line
            for line in FIXTURE.read_text().splitlines()
            if "Invented Test" not in line
        )
        with pytest.raises(ValueError, match="Holdings do not reconcile"):
            parse_layout(text)

    def test_inconsistent_dates_refused(self) -> None:
        with pytest.raises(ValueError, match="dates"):
            parse_layout(FIXTURE.read_text().replace("12 Sep 2026", "13 Sep 2026", 1))

    def test_duplicate_holding_refused(self) -> None:
        text = FIXTURE.read_text()
        row = next(line for line in text.splitlines() if "Invented Test" in line)
        with pytest.raises(ValueError, match="Duplicate holding"):
            parse_layout(text.replace(row, row + "\n" + row))

    def test_grouped_amounts_and_negative_cash(self) -> None:
        text = (
            FIXTURE.read_text()
            .replace("US$40", "US$1,040")
            .replace("US$43", "US$1,038")
            .replace("US$3\n", "US$-2\n")
            .replace("US$3  6.98%", "US$-2  6.98%")
        )
        assert parse_layout(text).sections[0].ending["cash"] == Decimal(-2)

    def test_blank_and_encrypted(self, tmp_path: Path) -> None:
        writer = PdfWriter()
        writer.add_blank_page(100, 100)
        path = tmp_path / "empty.pdf"
        writer.write(path)
        with pytest.raises(ValueError):
            extract_pdf(path)
        writer.encrypt("synthetic-password")
        writer.write(path)
        with pytest.raises(ValueError, match="Encrypted"):
            extract_pdf(path)


class TestReconciliation:
    def test_average_cost_and_cash_untouched(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        buy = Trade(
            security_id,
            "2026-09-01",
            "BUY",
            3,
            30,
            account_id=account_id,
            is_synthetic=True,
        )
        insert_trade(conn, buy)
        insert_trade(
            conn,
            replace(
                buy,
                side="SELL",
                quantity=1,
                amount_eur=15,
                trade_date="2026-09-02",
                is_synthetic=False,
            ),
        )
        before = (positions(conn), cash_eur(conn), conn.total_changes)
        result = reconcile(conn, parse_layout(FIXTURE.read_text()))
        assert result["rows"][0]["status"] == "quantity_match"
        assert "current" in result["ledger_basis"]
        assert "not verified" in result["identity_basis"]
        assert (positions(conn), cash_eur(conn), conn.total_changes) == before
        assert before[0][0].cost_basis_eur == 20

    def test_missing_extra_and_difference(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        statement = parse_layout(FIXTURE.read_text())
        assert reconcile(conn, statement)["rows"][0]["status"] == "only_in_statement"
        insert_trade(
            conn, Trade(security_id, "2026-09-01", "BUY", 1, 10, account_id=account_id)
        )
        extra = upsert_security(conn, Security(ticker="OTHER", name="Invented Other"))
        insert_trade(
            conn, Trade(extra, "2026-09-01", "BUY", 1, 10, account_id=account_id)
        )
        assert [r["status"] for r in reconcile(conn, statement)["rows"]] == [
            "only_in_ledger",
            "quantity_difference",
        ]

    def test_precision_and_currency_mismatch(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        insert_trade(
            conn,
            Trade(
                security_id, "2026-09-01", "BUY", 2.000000001, 20, account_id=account_id
            ),
        )
        statement = parse_layout(FIXTURE.read_text())
        assert reconcile(conn, statement)["rows"][0]["status"] == "quantity_match"
        conn.execute("UPDATE securities SET currency='EUR' WHERE id=?", (security_id,))
        assert {r["status"] for r in reconcile(conn, statement)["rows"]} == {
            "only_in_statement",
            "only_in_ledger",
        }


def seed(
    conn: sqlite3.Connection,
    account_id: int,
    security_id: int,
    quantity: float = 2.0,
    day: str = "2026-09-02",
) -> None:
    """Write an invented synthetic opening BUY and its seed snapshot row."""
    insert_trade(
        conn,
        Trade(
            security_id,
            day,
            "BUY",
            quantity,
            30,
            account_id=account_id,
            is_synthetic=True,
            note="Synthetic opening position.",
        ),
    )
    save_snapshot(
        conn,
        account_id=account_id,
        snapshot_date=day,
        cash_eur=5,
        positions=[(security_id, quantity, 40.0, 33.3)],
        source="revolut",
    )


def statement_with(quantity: str) -> Statement:
    """The invented statement with its one holding at another quantity."""
    return parse_layout(FIXTURE.read_text().replace("2.00000000", quantity))


class TestSeedCorrection:
    def test_truncation_corrected_without_moving_cost_or_cash(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        seed(conn, account_id, security_id)
        statement = statement_with("2.00567800")
        cash_before = cash_eur(conn)
        corrections, refusals = seed_corrections(conn, statement)
        assert refusals == []
        assert [c.after for c in corrections] == [Decimal("2.00567800")]
        assert corrections[0].value_added == Decimal("0.11356")

        apply_seed_corrections(conn, corrections, "ab" * 32)

        (position,) = positions(conn)
        assert position.quantity == pytest.approx(2.005678)
        assert position.cost_basis_eur == 30
        assert cash_eur(conn) == cash_before
        _, rows = latest_snapshot(conn, account_id=account_id)
        assert rows[security_id]["quantity"] == pytest.approx(2.005678)
        assert rows[security_id]["value_eur"] == 40
        note = load_trades(conn)[0].note
        assert note.startswith("Synthetic opening position.")
        assert "abababababab" in note
        assert reconcile(conn, statement)["rows"][0]["status"] == "quantity_match"
        assert seed_corrections(conn, statement) == ([], [])

    def test_rounding_is_not_truncation(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        seed(conn, account_id, security_id, quantity=2.01)
        corrections, refusals = seed_corrections(conn, statement_with("2.00567800"))
        assert corrections == []
        assert "truncation" in refusals[0]["reason"]

    def test_recorded_trade_refused(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        seed(conn, account_id, security_id, quantity=1.0)
        insert_trade(
            conn, Trade(security_id, "2026-09-05", "BUY", 1, 10, account_id=account_id)
        )
        corrections, refusals = seed_corrections(conn, statement_with("2.00567800"))
        assert corrections == []
        assert "beyond its synthetic opening" in refusals[0]["reason"]

    def test_statement_before_seed_refused(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        seed(conn, account_id, security_id, day="2026-10-05")
        corrections, refusals = seed_corrections(conn, statement_with("2.00567800"))
        assert corrections == []
        assert "before the seed date" in refusals[0]["reason"]

    def test_one_sided_holdings_refused(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        other = upsert_security(conn, Security(ticker="OTHER", name="Invented Other"))
        seed(conn, account_id, other)
        corrections, refusals = seed_corrections(conn, statement_with("2.00567800"))
        assert corrections == []
        assert [r["reason"].split(";")[0] for r in refusals] == [
            "only in the ledger",
            "only in the statement",
        ]

    def test_stale_batch_writes_nothing(
        self, conn: sqlite3.Connection, account_id: int, security_id: int
    ) -> None:
        seed(conn, account_id, security_id)
        corrections, _ = seed_corrections(conn, statement_with("2.00567800"))
        stale = replace(corrections[0], trade_id=999)
        with pytest.raises(ValueError, match="changed"):
            apply_seed_corrections(conn, [*corrections, stale], "ab" * 32)
        assert positions(conn)[0].quantity == 2.0
        _, rows = latest_snapshot(conn, account_id=account_id)
        assert rows[security_id]["quantity"] == 2.0


class TestArchive:
    def test_idempotent_round_trip_and_profile_isolation(self, tmp_path: Path) -> None:
        source = synthetic_pdf(tmp_path / "source.pdf", FIXTURE.read_text())
        profile = Profile("invented", 1, tmp_path / "profiles" / "invented")
        statement, directory = ingest(profile, source)
        assert ingest(profile, source)[1] == directory
        payload = json.loads((directory / "extracted-v1.json").read_text())
        assert payload["statement"] == statement.to_dict()
        assert (directory / "statement.pdf").read_bytes() == source.read_bytes()
        assert (directory / "statement.pdf").stat().st_mode & 0o777 == 0o600
        assert not profile.db.exists()
        other = replace(profile, name="other", root=tmp_path / "profiles" / "other")
        assert ingest(other, source)[1] != directory

    def test_repository_and_symlink_rejected(self, tmp_path: Path) -> None:
        repo = Path(__file__).resolve().parents[1]
        with pytest.raises(ValueError, match="outside"):
            private_path(repo / "private.pdf")
        link = tmp_path / "link"
        link.symlink_to(repo, target_is_directory=True)
        with pytest.raises(ValueError, match="outside"):
            private_path(link / "private.pdf")

    def test_bad_pdf_leaves_no_archive(self, tmp_path: Path) -> None:
        source = synthetic_pdf(tmp_path / "bad.pdf", "Not an account statement")
        profile = Profile("invented", 1, tmp_path / "profile")
        with pytest.raises(ValueError):
            ingest(profile, source)
        assert not profile.root.exists()


class TestDownloader:
    def test_session_reuse_is_explicit(self, tmp_path: Path) -> None:
        from revolut_download import session_path

        profile = Profile("invented", 1, tmp_path / "person")
        old = tmp_path / "old-browser-profile"
        old.mkdir()
        (old / "Local State").write_text("{}")
        assert (
            session_path(profile, None) == profile.root / "revolut" / "browser-profile"
        )
        assert session_path(profile, old) == old
        with pytest.raises(ValueError, match="Chrome profile"):
            session_path(profile, tmp_path / "missing")

    def test_requested_period_must_match(self) -> None:
        from revolut_download import validate_period

        statement = parse_layout(FIXTURE.read_text())
        validate_period(statement, date(2026, 9, 1), date(2026, 9, 30))
        with pytest.raises(ValueError, match="different period"):
            validate_period(statement, date(2026, 8, 1), date(2026, 8, 31))

    def test_invalid_range_does_not_open_browser(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio
        import sys
        from types import ModuleType

        from revolut_download import download_statement

        fake = ModuleType("playwright.async_api")
        fake.Download = fake.Page = fake.Response = object
        fake.Error = RuntimeError
        fake.TimeoutError = TimeoutError
        fake.async_playwright = lambda: pytest.fail("must validate before launch")
        monkeypatch.setitem(sys.modules, "playwright.async_api", fake)
        profile = Profile("invented", 1, tmp_path / "person")
        with pytest.raises(ValueError, match="both"):
            asyncio.run(download_statement(profile, start=date(2026, 9, 1)))
        assert not profile.root.exists()


class TestRevolutCommand:
    def test_missing_db_refuses_before_archive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import cmd_revolut
        from main import build_parser

        profile = Profile("invented", 1, tmp_path / "person")
        monkeypatch.setattr(
            cmd_revolut, "resolve_cli_profile", lambda name: (profile, profile.db)
        )
        args = build_parser().parse_args(
            ["revolut", "ingest", str(tmp_path / "unused.pdf")]
        )
        with pytest.raises(FileNotFoundError, match="init"):
            args.func(args)
        assert not profile.root.exists()

    def test_correct_seed_asks_before_writing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import cmd_revolut
        from main import build_parser

        profile = Profile("invented", 1, tmp_path / "person")
        profile.root.mkdir(parents=True)
        conn = open_db(profile.db)
        account = ensure_account(
            conn,
            Account(
                name="revolut", broker="Revolut", currency="EUR", sync_mode="manual"
            ),
        )
        security = upsert_security(
            conn, Security(ticker="TEST", name="Invented Test", currency="USD")
        )
        seed(conn, account, security)
        conn.close()
        pdf = synthetic_pdf(
            tmp_path / "statement.pdf",
            FIXTURE.read_text().replace("2.00000000", "2.00567800"),
        )
        monkeypatch.setattr(
            cmd_revolut, "resolve_cli_profile", lambda name: (profile, profile.db)
        )
        args = build_parser().parse_args(["revolut", "correct-seed", str(pdf)])

        def quantity() -> float:
            with closing(open_db(profile.db)) as check:
                return positions(check)[0].quantity

        monkeypatch.setattr("builtins.input", lambda prompt: "n")
        args.func(args)
        assert quantity() == 2.0
        monkeypatch.setattr("builtins.input", lambda prompt: "y")
        args.func(args)
        assert quantity() == pytest.approx(2.005678)
        assert list((profile.root / "revolut" / "statements").iterdir())


class TestBrowserLifecycle:
    @pytest.mark.parametrize(
        "outcome",
        ["success", "wrong_period", "timeout", "preview", "preview_error", "expired"],
    )
    def test_capture_validation_and_cleanup(
        self, outcome: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio
        import builtins
        import sys
        from types import ModuleType

        import revolut_download

        source = synthetic_pdf(tmp_path / "source.pdf", FIXTURE.read_text())
        profile = Profile("invented", 1, tmp_path / "person")
        observed = {}

        class Download:
            async def save_as(self, target: Path) -> None:
                target.write_bytes(source.read_bytes())

        class Response:
            url = "https://invest.revolut.com/api/retail/trading/accounts/synthetic/statements/account-statement"
            ok = True
            headers = {"content-type": "application/pdf"}

            async def json(self) -> dict:
                return {
                    "state": "ready",
                    "url": "https://storage.googleapis.com/synthetic/statement.pdf?signature=synthetic",
                    "requestId": "synthetic",
                }

            async def body(self) -> bytes:
                return (
                    b"invalid PDF"
                    if outcome == "preview_error"
                    else source.read_bytes()
                )

            async def dispose(self) -> None:
                observed["disposed"] = True

        class Request:
            async def get(self, url: str, **kwargs: object) -> Response:
                observed["fetched"] = url
                return Response()

        class Page:
            url = "https://invest.revolut.com/home"
            context = type("PageContext", (), {"request": Request()})()

            def get_by_role(self, *args: object, **kwargs: object) -> Page:
                return self

            async def wait_for(self, **kwargs: object) -> None:
                if outcome == "expired" and not observed.get("auth_waited"):
                    observed["auth_waited"] = True
                    raise TimeoutError

            def on(self, event: str, callback: object) -> None:
                observed[event] = callback

            async def goto(self, url: str, **kwargs: object) -> None:
                observed["url"] = url
                if outcome in {"success", "wrong_period"}:
                    asyncio.get_running_loop().call_soon(
                        observed["download"], Download()
                    )

        class Context:
            pages = [Page()]

            def set_default_timeout(self, value: int) -> None:
                observed["timeout"] = value

            def on(self, event: str, callback: object) -> None:
                pass

            async def close(self) -> None:
                observed["closed"] = True

        class Browser:
            async def launch_persistent_context(
                self, path: str, **kwargs: object
            ) -> Context:
                observed["session"] = path
                observed.update(kwargs)
                return Context()

        class Playwright:
            chromium = Browser()

            async def __aenter__(self) -> Playwright:
                return self

            async def __aexit__(self, *args: object) -> None:
                pass

        fake = ModuleType("playwright.async_api")
        fake.Download = Download
        fake.Page = Page
        fake.Response = Response
        fake.Error = RuntimeError
        fake.TimeoutError = TimeoutError
        fake.async_playwright = Playwright
        monkeypatch.setitem(sys.modules, "playwright.async_api", fake)
        monkeypatch.setattr(builtins, "input", lambda *args: "")
        monkeypatch.setattr(revolut_download, "REVOLUT_DOWNLOAD_TIMEOUT_MS", 50)

        async def navigate(page: Page, start: date, end: date) -> None:
            observed["navigated"] = True
            if outcome == "expired":
                observed["download"](Download())
            if outcome in {"preview", "preview_error"}:
                observed["response"](Response())

        monkeypatch.setattr(revolut_download, "navigate_statement", navigate)
        start = date(2026, 8, 1) if outcome == "wrong_period" else date(2026, 9, 1)
        operation = revolut_download.download_statement(
            profile,
            start=start,
            end=date(2026, 8, 31) if outcome == "wrong_period" else date(2026, 9, 30),
        )
        if outcome in {"success", "preview", "expired"}:
            statement, archive = asyncio.run(operation)
            assert statement.generated_on == date(2026, 9, 12)
            assert (archive / "statement.pdf").is_file()
        else:
            with pytest.raises(ValueError):
                asyncio.run(operation)
            assert not (profile.root / "revolut" / "statements").exists()
        assert observed["chromium_sandbox"] is True
        assert observed["headless"] is False
        assert observed["closed"] is True
        assert observed["session"] == str(profile.root / "revolut" / "browser-profile")
        assert not profile.db.exists()


class TestRequestedPeriod:
    def test_defaults_and_future_custom_range(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import revolut_navigation

        class Today(date):
            @classmethod
            def today(cls) -> date:
                return date(2026, 9, 12)

        monkeypatch.setattr(revolut_navigation, "date", Today)
        assert revolut_navigation.requested_period(None, None) == (
            date(2026, 9, 1),
            date(2026, 9, 30),
        )
        assert revolut_navigation.requested_period(
            date(2026, 8, 1), date(2026, 8, 31)
        ) == (date(2026, 8, 1), date(2026, 8, 31))
        for start, end in [
            (date(2026, 9, 1), None),
            (date(2026, 10, 1), date(2026, 10, 31)),
            (date(2026, 9, 2), date(2026, 9, 30)),
            (date(2026, 9, 12), date(2026, 9, 1)),
        ]:
            with pytest.raises(ValueError):
                revolut_navigation.requested_period(start, end)

    def test_leap_month(self) -> None:
        from revolut_navigation import whole_month

        assert whole_month(date(2024, 2, 1), date(2024, 2, 29))
        assert not whole_month(date(2024, 2, 1), date(2024, 2, 28))


class TestMonthPeriod:
    """`/refresh last` has to name a whole month; partials would overlap."""

    def test_last_and_named_months(self) -> None:
        from revolut_navigation import month_period

        today = date(2026, 9, 14)
        assert month_period("last", today=today) == (
            date(2026, 8, 1),
            date(2026, 8, 31),
        )
        assert month_period("2026-02", today=today) == (
            date(2026, 2, 1),
            date(2026, 2, 28),
        )
        assert month_period("LAST", today=today)[0] == date(2026, 8, 1)

    def test_january_rolls_back_a_year(self) -> None:
        from revolut_navigation import month_period

        assert month_period("last", today=date(2026, 1, 9)) == (
            date(2025, 12, 1),
            date(2025, 12, 31),
        )

    def test_leap_february_is_whole(self) -> None:
        from revolut_navigation import month_period, whole_month

        start, end = month_period("2024-02", today=date(2026, 9, 14))
        assert end == date(2024, 2, 29) and whole_month(start, end)

    @pytest.mark.parametrize(
        "token", ["yesterday", "2026-13", "2026-8", "08-2026", "", "2026-12"]
    )
    def test_unusable_tokens_are_refused(self, token: str) -> None:
        from revolut_navigation import month_period

        with pytest.raises(ValueError):
            month_period(token, today=date(2026, 9, 14))


class TestConsentBanner:
    """A banner over the profile menu ate the first click of a live refresh."""

    def _dismiss(self, page: object) -> bool:
        import asyncio

        from revolut_navigation import dismiss_consent_banner

        return asyncio.run(dismiss_consent_banner(page))

    def test_a_showing_banner_is_dismissed(self) -> None:
        page = StubPage(
            {
                "home": {
                    "url": "https://invest.revolut.com/home",
                    "buttons": {"Reject all cookies", "Open profile"},
                    "qr": None,
                },
                "clean": {
                    "url": "https://invest.revolut.com/home",
                    "buttons": {"Open profile"},
                    "qr": None,
                },
            },
            start="home",
            on_click={"Reject all cookies": "clean"},
        )
        assert self._dismiss(page) is True
        assert page.current == "clean"

    def test_no_banner_is_not_an_error(self) -> None:
        page = StubPage(
            {
                "clean": {
                    "url": "https://invest.revolut.com/home",
                    "buttons": {"Open profile"},
                    "qr": None,
                }
            },
            start="clean",
        )
        assert self._dismiss(page) is False

    def test_a_raising_page_never_fails_the_download(self) -> None:
        class Broken:
            def get_by_role(self, *a: object, **k: object):
                raise RuntimeError("page closed")

        assert self._dismiss(Broken()) is False


class StubLocator:
    """One button or text link on a StubPage, resolved by exact name."""

    def __init__(self, page: StubPage, name: str, key: str = "buttons") -> None:
        self._page = page
        self._name = name
        self._key = key

    @property
    def first(self) -> StubLocator:
        return self

    async def is_visible(self) -> bool:
        screen = self._page.screens[self._page.current]
        return self._name in screen.get(self._key, set())

    async def click(self) -> None:
        nxt = self._page.on_click.get(self._name)
        if nxt is not None:
            self._page.current = nxt


class StubPage:
    """A scripted Revolut sign-in, transitioning on clicks and delivery."""

    def __init__(
        self,
        screens: dict[str, dict],
        start: str,
        on_click: dict[str, str] | None = None,
    ) -> None:
        self.screens = screens
        self.current = start
        self.on_click = on_click or {}

    @property
    def url(self) -> str:
        return self.screens[self.current]["url"]

    def get_by_role(self, role: str, *, name: str, exact: bool) -> StubLocator:
        assert role == "button" and exact
        return StubLocator(self, name)

    def get_by_text(self, text: str, *, exact: bool) -> StubLocator:
        assert exact
        return StubLocator(self, text, key="texts")

    async def screenshot(self) -> bytes:
        return self.screens[self.current].get("qr") or b""


class TestRelaySignIn:
    LANDING = "Log in with Revolut"
    PORTFOLIO = "Open profile"

    def _clock(self, step: float = 4.0):
        counter = {"t": 0.0}

        def now() -> float:
            value = counter["t"]
            counter["t"] += step
            return value

        return now

    def _run(self, page: StubPage, deliver, **kwargs):
        import asyncio

        import revolut_login

        return asyncio.run(
            revolut_login.relay_sign_in(
                page, deliver=deliver, now=self._clock(), **kwargs
            )
        )

    @pytest.fixture(autouse=True)
    def _no_real_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def instant(_seconds: float) -> None:
            return None

        monkeypatch.setattr("revolut_login._sleep", instant)

    @pytest.fixture
    def _decoder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # State-machine tests do not depend on a real QR image.
        monkeypatch.setattr(
            "revolut_login.decode_login_qr",
            lambda png: "https://revolut.com/app?token=synthetic" if png else None,
        )

    def test_already_signed_in_returns_at_once(self, _decoder: None) -> None:
        page = StubPage(
            {
                "home": {
                    "url": "https://invest.revolut.com/home",
                    "buttons": {self.PORTFOLIO},
                    "qr": None,
                }
            },
            start="home",
        )
        sent: list[str] = []

        async def deliver(link: str) -> None:
            sent.append(link)

        outcome = self._run(page, deliver)
        assert outcome.ok and outcome.status == "signed_in"
        assert sent == []

    def test_cold_start_relays_qr_then_signs_in(self, _decoder: None) -> None:
        sent: list[str] = []

        async def deliver(link: str) -> None:
            sent.append(link)
            page.current = "home"  # phone approval reaches the portfolio

        page = StubPage(
            {
                "landing": {
                    "url": "https://invest.revolut.com/",
                    "buttons": {self.LANDING},
                    "qr": None,
                },
                "signin": {
                    "url": "https://sso.revolut.com/signin",
                    "buttons": set(),
                    "qr": b"QR",
                },
                "home": {
                    "url": "https://invest.revolut.com/home",
                    "buttons": {self.PORTFOLIO},
                    "qr": None,
                },
            },
            start="landing",
            on_click={self.LANDING: "signin"},
        )
        outcome = self._run(page, deliver, timeout_s=100)
        assert outcome.ok
        assert sent == ["https://revolut.com/app?token=synthetic"]

    def test_slow_rendering_landing_is_not_abandoned(self, _decoder: None) -> None:
        # Regression: domcontentloaded fires before the landing button mounts,
        # so the first poll sees an empty page. It must keep polling, not bail
        # to needs_attention. Observed live: the daemon reported sign-in needed
        # in seconds against a landing page that simply had not rendered yet.
        state = {"screen": "landing", "polls": 0}
        sent: list[str] = []

        async def deliver(link: str) -> None:
            sent.append(link)
            state["screen"] = "home"

        outer = self

        class SlowPage:
            @property
            def url(self) -> str:
                return {
                    "landing": "https://invest.revolut.com/",
                    "signin": "https://sso.revolut.com/signin",
                    "home": "https://invest.revolut.com/home",
                }[state["screen"]]

            def get_by_role(self, role: str, *, name: str, exact: bool):
                class Loc:
                    async def is_visible(self) -> bool:
                        if name == outer.PORTFOLIO:
                            state["polls"] += 1
                            return state["screen"] == "home"
                        if name == outer.LANDING:
                            # The button only mounts after the first poll.
                            return state["screen"] == "landing" and state["polls"] >= 2
                        return False

                    async def click(self) -> None:
                        if name == outer.LANDING:
                            state["screen"] = "signin"

                return Loc()

            async def screenshot(self) -> bytes:
                return b"QR" if state["screen"] == "signin" else b""

        outcome = self._run(SlowPage(), deliver, timeout_s=100)
        assert outcome.ok and outcome.status == "signed_in"
        assert sent == ["https://revolut.com/app?token=synthetic"]

    def test_passcode_screen_clicks_not_you_then_relays_qr(
        self, _decoder: None
    ) -> None:
        # Regression: an inactivity logout shows a passcode-first screen with no
        # QR. The relay must click "Not you?" to reach the QR page rather than
        # give up or need the passcode. Observed live at sso.revolut.com/passcode.
        sent: list[str] = []

        async def deliver(link: str) -> None:
            sent.append(link)
            page.current = "home"

        page = StubPage(
            {
                "passcode": {
                    "url": "https://sso.revolut.com/passcode",
                    "buttons": set(),
                    "texts": {"Not you?"},
                    "qr": None,
                },
                "signin": {
                    "url": "https://sso.revolut.com/signin",
                    "buttons": set(),
                    "qr": b"QR",
                },
                "home": {
                    "url": "https://invest.revolut.com/home",
                    "buttons": {self.PORTFOLIO},
                    "qr": None,
                },
            },
            start="passcode",
            on_click={"Not you?": "signin"},
        )
        outcome = self._run(page, deliver, timeout_s=100)
        assert outcome.ok and outcome.status == "signed_in"
        assert sent == ["https://revolut.com/app?token=synthetic"]

    def test_qr_delivered_once(self, _decoder: None) -> None:
        sent: list[str] = []
        page = StubPage(
            {
                "signin": {
                    "url": "https://sso.revolut.com/signin",
                    "buttons": set(),
                    "qr": b"QR",
                }
            },
            start="signin",
        )

        async def deliver(link: str) -> None:
            sent.append(link)

        outcome = self._run(page, deliver, timeout_s=20)
        assert outcome.status == "timeout"
        assert sent == ["https://revolut.com/app?token=synthetic"]

    def test_delivery_failure_is_needs_attention(self, _decoder: None) -> None:
        async def deliver(_link: str) -> None:
            raise RuntimeError("telegram down")

        page = StubPage(
            {
                "signin": {
                    "url": "https://sso.revolut.com/signin",
                    "buttons": set(),
                    "qr": b"QR",
                }
            },
            start="signin",
        )
        outcome = self._run(page, deliver, timeout_s=100)
        assert outcome.status == "needs_attention"
        assert not outcome.ok

    def test_unexpected_screen_reports_attention(self, _decoder: None) -> None:
        page = StubPage(
            {
                "passcode": {
                    "url": "https://sso.revolut.com/passcode",
                    "buttons": {"Forgot passcode?"},
                    "qr": None,
                }
            },
            start="passcode",
        )

        async def deliver(_link: str) -> None:
            raise AssertionError("should not be called on an unexpected screen")

        outcome = self._run(page, deliver, timeout_s=100)
        assert outcome.status == "needs_attention"

    def test_real_qr_round_trip_decodes_a_revolut_link(self) -> None:
        zxingcpp = pytest.importorskip("zxingcpp")
        pytest.importorskip("PIL")
        import io

        from PIL import Image

        from revolut_login import decode_login_qr

        link = "https://revolut.com/app/login?token=abc123"
        barcode = zxingcpp.create_barcode(link, zxingcpp.BarcodeFormat.QRCode)
        image = barcode.to_image(scale=6)
        if not isinstance(image, Image.Image):
            import numpy as np

            image = Image.fromarray(np.asarray(image))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        assert decode_login_qr(buffer.getvalue()) == link
        # A non-Revolut QR is never relayed.
        other = zxingcpp.create_barcode(
            "https://example.com/x", zxingcpp.BarcodeFormat.QRCode
        ).to_image(scale=6)
        if not isinstance(other, Image.Image):
            import numpy as np

            other = Image.fromarray(np.asarray(other))
        buffer = io.BytesIO()
        other.save(buffer, format="PNG")
        assert decode_login_qr(buffer.getvalue()) is None
