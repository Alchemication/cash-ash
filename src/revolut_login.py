"""Drive the Revolut web sign-in and relay its QR for phone approval.

Sign-in is QR-only: the cold-start screens are the ``invest.revolut.com``
landing page (button "Log in with Revolut"), then ``sso.revolut.com/signin``
showing a login QR, then the portfolio once the phone approves. No passcode is
entered, so none is stored. This module clicks the landing button, reads the QR
from a page screenshot, hands the link to a caller-supplied ``deliver`` (which
sends it to Telegram), and waits for the portfolio to load.

Every other screen — an inactivity logout that asks for a passcode first, an
extra identity check, a decoded QR that is not a Revolut link — resolves to
``needs_attention`` rather than a hang, so a scheduled run can report "sign-in
needs you at the Mac" instead of leaving Chrome open on an empty desk.

The QR link carries a short-lived login token. It is never logged; only its
scheme and host are, and only a ``revolut.com`` link is ever relayed.

Public API:
    SignInOutcome  -- the result: signed_in, needs_attention or timeout
    decode_login_qr -- pull an https revolut.com link out of a screenshot
    relay_sign_in  -- drive the screens, delivering the QR once
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from config import REVOLUT_SIGN_IN_POLL_S, REVOLUT_SIGN_IN_TIMEOUT_S

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)

_LANDING_BUTTON = "Log in with Revolut"
_PORTFOLIO_BUTTON = "Open profile"
_SIGN_IN_HOST = "sso.revolut.com"
_RESET_USER_LINK = "Not you?"
"""On an inactivity logout Revolut shows a passcode-first screen bound to the
remembered user, with no QR. This link drops back to the QR sign-in page, so the
relay can reach a QR without ever entering or storing the passcode."""


@dataclass(frozen=True)
class SignInOutcome:
    """The result of a relayed sign-in.

    Attributes:
        status: ``signed_in`` when the portfolio loaded; ``timeout`` when the
            QR was relayed but never approved in time; ``needs_attention`` for
            any screen this flow cannot drive unattended.
        detail: A short human-readable reason, safe to log and to show — it
            never contains the login token.
    """

    status: str
    detail: str

    @property
    def ok(self) -> bool:
        """True only when sign-in reached the portfolio."""
        return self.status == "signed_in"


def decode_login_qr(png: bytes) -> str | None:
    """Return the Revolut login link in a screenshot, or None.

    Only an ``https`` link on a ``revolut.com`` host is returned, so a foreign
    QR that happens to be on screen is never relayed. Decoding is best-effort:
    anything unreadable is treated as "no QR yet", not an error.

    Args:
        png: PNG bytes of the current page.

    Returns:
        The login URL, or None when no Revolut QR is present.
    """
    try:
        import io

        import zxingcpp
        from PIL import Image
    except ImportError:  # pragma: no cover - the browser group is installed to use this
        logger.warning("QR decoding needs the 'browser' dependency group.")
        return None
    try:
        image = Image.open(io.BytesIO(png))
        results = zxingcpp.read_barcodes(image)
    except Exception as exc:  # noqa: BLE001 - a bad frame is just "no QR yet"
        logger.debug("QR read failed on this frame: %s", type(exc).__name__)
        return None
    for result in results:
        link = result.text or ""
        parts = urlsplit(link)
        host = parts.hostname or ""
        if parts.scheme == "https" and (
            host == "revolut.com" or host.endswith(".revolut.com")
        ):
            return link
    return None


async def relay_sign_in(
    page: Page,
    *,
    deliver: Callable[[str], Awaitable[None]],
    timeout_s: int = REVOLUT_SIGN_IN_TIMEOUT_S,
    poll_s: float = REVOLUT_SIGN_IN_POLL_S,
    now: Callable[[], float] = monotonic,
) -> SignInOutcome:
    """Sign in by relaying the login QR, returning without a terminal prompt.

    Drives the known cold-start screens: an already-authenticated session
    returns at once; the landing page is advanced by its button; the sign-in
    page's QR is decoded and passed to ``deliver`` exactly once. Any screen that
    is neither of those, and is not yet the portfolio, ends the attempt as
    ``needs_attention`` — including a passcode-first logout, which has no
    relayable QR.

    Args:
        page: The Playwright page, already navigated to invest.revolut.com.
        deliver: Async callback given the QR link; sends it to the owner. Called
            once per attempt. Its own failure ends the attempt as
            ``needs_attention``.
        timeout_s: Seconds to wait for approval after the QR is delivered.
        poll_s: Seconds between screen checks.
        now: Monotonic clock, injectable for tests.

    Returns:
        The outcome. ``needs_attention`` and ``timeout`` both mean the caller
        should tell the owner to sign in at the Mac.
    """
    deadline = now() + timeout_s
    delivered = False
    landing_clicked = False
    reset_clicked = False
    while now() < deadline:
        if await _visible(page, _PORTFOLIO_BUTTON):
            return SignInOutcome("signed_in", "Reached the portfolio.")

        # Never bail on an unrecognised screen: after ``domcontentloaded`` the
        # landing button and the QR both mount a moment later, so a first poll
        # that sees neither is the page still rendering, not a wrong screen.
        # Keep driving what appears until the deadline, then classify once.
        host = urlsplit(page.url).hostname or ""
        logger.debug("Sign-in poll: host=%s delivered=%s", host, delivered)
        if (
            not delivered
            and not landing_clicked
            and await _visible(page, _LANDING_BUTTON)
        ):
            await _click(page, _LANDING_BUTTON)
            landing_clicked = True
        elif not delivered and host == _SIGN_IN_HOST:
            link = decode_login_qr(await page.screenshot())
            if link is not None:
                parts = urlsplit(link)
                logger.info(
                    "Login QR found (%s://%s); relaying for phone approval.",
                    parts.scheme,
                    parts.hostname,
                )
                try:
                    await deliver(link)
                except Exception as exc:  # noqa: BLE001 - delivery is the caller's channel
                    return SignInOutcome(
                        "needs_attention",
                        f"Could not send the login link ({type(exc).__name__}); "
                        "sign in at the Mac.",
                    )
                delivered = True
            elif not reset_clicked and await _text_visible(page, _RESET_USER_LINK):
                # Passcode-first logout screen: escape to the QR page rather than
                # entering the passcode. Done once, so a page that keeps the link
                # cannot loop.
                logger.info(
                    "Passcode screen; clicking %r to reach the QR sign-in.",
                    _RESET_USER_LINK,
                )
                await _click_text(page, _RESET_USER_LINK)
                reset_clicked = True
        await _sleep(poll_s)

    if delivered:
        return SignInOutcome(
            "timeout",
            "Login link was sent but not approved in time; run again and approve "
            "on the phone within the window.",
        )
    return SignInOutcome(
        "needs_attention", "Could not find a login QR to relay; sign in at the Mac."
    )


async def _visible(page: Page, name: str) -> bool:
    """Whether a button with an exact accessible name is on screen."""
    try:
        return await page.get_by_role("button", name=name, exact=True).is_visible()
    except Exception:  # noqa: BLE001 - a mid-navigation query is just "not yet"
        return False


async def _text_visible(page: Page, text: str) -> bool:
    """Whether an element with exactly this text is on screen.

    Used for ``Not you?``, which Revolut renders as a link rather than a button,
    so the button-role query would miss it.
    """
    try:
        return await page.get_by_text(text, exact=True).first.is_visible()
    except Exception:  # noqa: BLE001 - a mid-navigation query is just "not yet"
        return False


async def _click_text(page: Page, text: str) -> None:
    """Click an element by its exact text, ignoring a lost race."""
    try:
        await page.get_by_text(text, exact=True).first.click()
    except Exception as exc:  # noqa: BLE001 - the next poll re-reads the screen
        logger.debug("Click on %r did not land: %s", text, type(exc).__name__)


async def _click(page: Page, name: str) -> None:
    """Click a button by its exact accessible name, ignoring a lost race."""
    try:
        await page.get_by_role("button", name=name, exact=True).click()
    except Exception as exc:  # noqa: BLE001 - the next poll re-reads the screen
        logger.debug("Click on %r did not land: %s", name, type(exc).__name__)


async def _sleep(seconds: float) -> None:
    """Async pause, wrapped so tests can patch one place."""
    import asyncio

    await asyncio.sleep(seconds)
