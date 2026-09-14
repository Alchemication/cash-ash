"""On-demand Chrome statement downloads with explicit session reuse."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from uuid import uuid4

from config import REVOLUT_BROWSER_TIMEOUT_MS, REVOLUT_DOWNLOAD_TIMEOUT_MS
from profiles import Profile
from revolut_login import SignInOutcome
from revolut_navigation import navigate_statement, requested_period
from revolut_statement import Statement, extract_pdf
from revolut_storage import ingest, private_path, revolut_root

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)

SignIn = Callable[["Page"], Awaitable[SignInOutcome]]
"""A strategy that drives the login screens and reports the outcome."""


class SignInRequired(Exception):
    """Raised when unattended sign-in could not reach the portfolio.

    Carries the :class:`~revolut_login.SignInOutcome` so the caller can tell the
    owner whether to approve on the phone again or sign in at the Mac.
    """

    def __init__(self, outcome: SignInOutcome) -> None:
        super().__init__(outcome.detail)
        self.outcome = outcome


def session_path(profile: Profile, reuse_session: Path | None) -> Path:
    """Select a dedicated profile or an explicitly supplied existing session."""
    if reuse_session is not None:
        path = private_path(reuse_session)
        if not path.is_dir() or not (path / "Local State").is_file():
            raise ValueError(
                "Session directory is not a Chrome profile; supply its browser-profile directory."
            )
        return path
    return private_path(revolut_root(profile) / "browser-profile")


def validate_period(statement: Statement, start: date | None, end: date | None) -> None:
    """Refuse a download for the wrong requested period."""
    if start is not None and (
        statement.period_start != start or statement.period_end != end
    ):
        raise ValueError(
            "Downloaded statement has a different period; select the requested dates and retry."
        )


async def download_statement(
    profile: Profile,
    *,
    reuse_session: Path | None = None,
    start: date | None = None,
    end: date | None = None,
    manual_navigation: bool = False,
    sign_in: SignIn | None = None,
) -> tuple[Statement, Path]:
    """Let the owner unlock Chrome and download a PDF into their private archive.

    No credentials are read or entered by CashAsh. Document controls use the
    verified English UI; manual navigation is an explicit fallback.

    Args:
        sign_in: How to get past the login screens. ``None`` is the attended
            default: wait for the portfolio, and if it does not appear, prompt
            for a terminal Enter once the owner has signed in by hand. Supplied
            (by the Telegram refresh flow) it drives sign-in unattended and
            raises :class:`SignInRequired` when the owner must act.
    """
    try:
        from playwright.async_api import (
            Download,
            Error,
            Response,
            async_playwright,
        )
        from playwright.async_api import TimeoutError as BrowserTimeout
    except ImportError as exc:
        raise ValueError(
            "Run with 'uv run --group browser python main.py revolut download'; Chrome must be installed."
        ) from exc
    start, end = requested_period(start, end)
    browser_path = session_path(profile, reuse_session)
    root = revolut_root(profile)
    downloads = private_path(root / "downloads")
    # Browser state and temporary download files are private from creation.
    previous_umask = os.umask(0o077)
    context = None
    tasks: set[asyncio.Task] = set()
    previews: set[str] = set()
    export_started = False
    try:
        downloads.mkdir(parents=True, exist_ok=True, mode=0o700)
        result: asyncio.Future[tuple[Statement, Path]] = (
            asyncio.get_running_loop().create_future()
        )

        def accept(target: Path) -> None:
            statement = extract_pdf(target)
            validate_period(statement, start, end)
            archived = ingest(profile, target)
            if not result.done():
                result.set_result(archived)

        async def save_preview(page: Page, url: str) -> None:
            """Save the signed PDF URL observed in Revolut's export popup."""
            target = private_path(downloads / f"{uuid4().hex}.pdf")
            response = None
            try:
                response = await page.context.request.get(
                    url, timeout=REVOLUT_DOWNLOAD_TIMEOUT_MS
                )
                if (
                    not response.ok
                    or response.headers.get("content-type", "").split(";")[0]
                    != "application/pdf"
                ):
                    raise ValueError(
                        "Revolut's preview did not return a PDF; retry the export."
                    )
                target.write_bytes(await response.body())
                accept(target)
            except (Error, OSError, ValueError) as exc:
                if not result.done():
                    result.set_exception(
                        ValueError(
                            f"PDF preview could not be saved ({type(exc).__name__}); retry the export."
                        )
                    )
            finally:
                if response is not None:
                    await response.dispose()

        async def save(item: Download) -> None:
            target = private_path(downloads / f"{uuid4().hex}.pdf")
            try:
                await item.save_as(target)
                accept(target)
            except (Error, OSError, ValueError) as exc:
                if not result.done():
                    result.set_exception(
                        ValueError(
                            f"Download could not be ingested ({type(exc).__name__}); inspect the private downloads directory and retry the PDF export."
                        )
                    )

        def queue_preview(page: Page, url: str) -> None:
            location = urlsplit(url)
            if (
                export_started
                and location.scheme == "https"
                and location.hostname == "storage.googleapis.com"
                and location.path.lower().endswith(".pdf")
                and url not in previews
            ):
                previews.add(url)
                task = asyncio.create_task(save_preview(page, url))
                tasks.add(task)
                task.add_done_callback(tasks.discard)

        async def export_response(page: Page, response: Response) -> None:
            """Use the URL returned by the observed UI export response.

            Chrome may block the asynchronous preview popup. Reading this
            response avoids dependence on the popup; no export API is called
            directly and signed URLs are never persisted or logged.
            """
            try:
                payload = await response.json()
                if isinstance(payload, dict) and isinstance(payload.get("url"), str):
                    queue_preview(page, payload["url"])
            except (Error, ValueError):
                logger.warning(
                    "Export response unavailable; waiting for a PDF preview or download."
                )

        def watch(page: Page) -> None:
            def on_navigation(frame: object = None) -> None:
                queue_preview(page, page.url)

            def on_response(response: Response) -> None:
                location = urlsplit(response.url)
                if (
                    export_started
                    and location.hostname == "invest.revolut.com"
                    and location.path.endswith("/statements/account-statement")
                    and response.ok
                ):
                    task = asyncio.create_task(export_response(page, response))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)

            def on_download(item: Download) -> None:
                if not export_started:
                    return
                task = asyncio.create_task(save(item))
                tasks.add(task)
                task.add_done_callback(tasks.discard)

            page.on("download", on_download)
            page.on("framenavigated", on_navigation)
            page.on("response", on_response)
            on_navigation()

        async with async_playwright() as playwright:
            try:
                context = await playwright.chromium.launch_persistent_context(
                    str(browser_path),
                    channel="chrome",
                    headless=False,
                    chromium_sandbox=True,
                    accept_downloads=True,
                )
                context.set_default_timeout(REVOLUT_BROWSER_TIMEOUT_MS)
                context.on("page", watch)
                for page in context.pages:
                    watch(page)
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(
                    "https://invest.revolut.com", wait_until="domcontentloaded"
                )
                logger.info(
                    "Session: %s. Complete any passcode, QR or phone approval directly in Chrome.",
                    browser_path,
                )
                if sign_in is not None:
                    outcome = await sign_in(page)
                    if not outcome.ok:
                        raise SignInRequired(outcome)
                else:
                    try:
                        await page.get_by_role(
                            "button", name="Open profile", exact=True
                        ).wait_for(state="visible")
                    except BrowserTimeout:
                        logger.info(
                            "Complete sign-in in Chrome, then press Enter in this terminal."
                        )
                        await asyncio.to_thread(input)
                        await page.get_by_role(
                            "button", name="Open profile", exact=True
                        ).wait_for(state="visible")
                logger.info("Statement period: %s through %s.", start, end)
                export_started = True
                if manual_navigation:
                    logger.info(
                        "In Chrome: avatar → Documents → Brokerage account → Account statement → PDF → month/custom dates → Get statement."
                    )
                else:
                    await navigate_statement(page, start, end)
                logger.info("Waiting for the exported PDF.")
                try:
                    return await asyncio.wait_for(
                        result, REVOLUT_DOWNLOAD_TIMEOUT_MS / 1000
                    )
                except TimeoutError as exc:
                    raise ValueError(
                        "No completed PDF download. Run again, unlock Chrome and choose the statement's Download button."
                    ) from exc
            except EOFError as exc:
                raise ValueError(
                    "Run the downloader in an interactive terminal so you can acknowledge sign-in."
                ) from exc
            except Error as exc:
                # Logged before it is wrapped: the message below says what to do
                # but not what broke, and a generic report cost a live debugging
                # round-trip when a cookie banner swallowed the first click.
                logger.warning("Chrome automation failed: %s", exc)
                raise ValueError(
                    "Chrome could not complete the download. Close the probe and other windows using this session, check Chrome is installed, then retry."
                ) from exc
            finally:
                if tasks:
                    pending = list(tasks)
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                if context is not None:
                    try:
                        await asyncio.wait_for(
                            context.close(), REVOLUT_BROWSER_TIMEOUT_MS / 1000
                        )
                    except TimeoutError:
                        logger.warning(
                            "Chrome shutdown timed out; close its remaining window before reusing the session."
                        )
    finally:
        os.umask(previous_umask)
