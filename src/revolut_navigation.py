"""English Revolut document controls verified in authenticated Chrome.

Only the account statement screens are automated. Authentication stays manual.
"""

from __future__ import annotations

import calendar
import logging
import re
from datetime import date, timedelta
from typing import TYPE_CHECKING

from config import REVOLUT_CALENDAR_MAX_STEPS

if TYPE_CHECKING:
    from playwright.async_api import Locator, Page

logger = logging.getLogger(__name__)

_MONTHS = "January February March April May June July August September October November December".split()
_SHORT_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sept Oct Nov Dec".split()


def requested_period(start: date | None, end: date | None) -> tuple[date, date]:
    """Default to the current calendar month; reject impossible custom dates."""
    if (start is None) != (end is None):
        raise ValueError("Supply both --start and --end in chronological order.")
    today = date.today()
    if start is None:
        start = today.replace(day=1)
        end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    assert end is not None
    if start > end or start > today:
        raise ValueError("Choose an ordered period starting no later than today.")
    if end > today and not whole_month(start, end):
        raise ValueError(
            "Custom ranges cannot end in the future; use a complete calendar month for a partial current-month statement."
        )
    return start, end


def month_period(token: str, *, today: date | None = None) -> tuple[date, date]:
    """Resolve a month token to one whole calendar month.

    Whole months are what the statement screen selects with its Month mode, and
    complete months do not overlap — which is what duplicate detection needs
    once transaction import exists. A partial range is deliberately not offered
    here: it is the thing that would make two statements double-count.

    Args:
        token: ``last`` for the previous calendar month, or ``YYYY-MM``.
        today: Reference date, injectable for tests.

    Returns:
        The first and last day of that month.

    Raises:
        ValueError: If the token is not a month, or has not started yet.
    """
    now = today or date.today()
    text = token.strip().lower()
    if text == "last":
        first = (now.replace(day=1) - timedelta(days=1)).replace(day=1)
    else:
        match = re.fullmatch(r"(\d{4})-(\d{2})", text)
        if match is None:
            raise ValueError(
                f"{token!r} is not a month. Send /refresh for this month, "
                f"/refresh last for the previous one, or /refresh YYYY-MM."
            )
        year, month = int(match.group(1)), int(match.group(2))
        if not 1 <= month <= 12:
            raise ValueError(f"{token!r} is not a month; months run 01 to 12.")
        first = date(year, month, 1)
    if first > now:
        raise ValueError(f"{token!r} has not started yet.")
    return first, first.replace(day=calendar.monthrange(first.year, first.month)[1])


def whole_month(start: date, end: date) -> bool:
    """Whether the requested dates describe one entire calendar month."""
    return (
        start.day == 1
        and start.year == end.year
        and start.month == end.month
        and end.day == calendar.monthrange(end.year, end.month)[1]
    )


async def _choose_date(picker: Locator, target: date, *, month: bool) -> None:
    """Navigate the verified month/year picker with a bounded number of steps."""
    from playwright.async_api import expect

    label = (
        f"{_MONTHS[target.month - 1]} {target.year}"
        if month
        else re.compile(
            rf"^\w{{3}}, {target.day:02d} {_SHORT_MONTHS[target.month - 1]} {target.year}$"
        )
    )
    caption_pattern = re.compile(
        r"^\d{4}$" if month else rf"^({'|'.join(_MONTHS)}) \d{{4}}$"
    )
    caption = picker.get_by_role("button", name=caption_pattern)
    for _ in range(REVOLUT_CALENDAR_MAX_STEPS):
        cell = picker.get_by_role("gridcell", name=label, exact=True)
        if await cell.count():
            if await cell.get_attribute("aria-disabled") == "true":
                raise ValueError(
                    "Requested date is disabled by Revolut; choose an available period."
                )
            await cell.click()
            return
        current = await caption.inner_text()
        if month:
            before = int(current) < target.year
        else:
            name, year = current.split()
            before = (int(year), _MONTHS.index(name) + 1) < (target.year, target.month)
        await picker.get_by_role(
            "button", name="Next" if before else "Prev", exact=True
        ).click()
        await expect(caption).not_to_have_text(current)
    raise ValueError(
        "Calendar navigation limit reached; choose a more recent period or use --manual-navigation."
    )


_CONSENT_BUTTONS = ("Reject all cookies", "Allow all cookies")
"""The consent banner a freshly signed-in session shows on the portfolio.

It overlays the profile menu, so the first document click is intercepted and
retried until the browser timeout — observed live as a refresh that signed in
cleanly and then failed thirty seconds later with a generic Chrome error.
Rejecting is the narrower choice, and the session cookie sign-in just set is
strictly necessary, so it survives.
"""


async def dismiss_consent_banner(page: Page) -> bool:
    """Dismiss the cookie banner if one is showing.

    Best-effort by design: no banner is the normal case on a session that has
    already answered it, and a banner that cannot be dismissed must not fail the
    download that follows.

    Args:
        page: The signed-in portfolio page.

    Returns:
        True if a banner was dismissed.
    """
    for name in _CONSENT_BUTTONS:
        try:
            button = page.get_by_role("button", name=name, exact=True).first
            if await button.is_visible():
                await button.click()
                logger.info("Dismissed the cookie banner with %r.", name)
                return True
        except Exception as exc:  # noqa: BLE001 - the banner is never worth failing over
            logger.debug("Consent banner check failed: %s", type(exc).__name__)
            return False
    return False


async def navigate_statement(page: Page, start: date, end: date) -> None:
    """Open PDF export and select the requested month or custom date range."""
    await dismiss_consent_banner(page)
    await page.get_by_role("button", name="Open profile", exact=True).click()
    await page.get_by_text("Documents", exact=True).click()
    await page.get_by_role("button", name="Brokerage account", exact=True).click()
    await page.get_by_text("Account statement", exact=True).click()
    await page.get_by_role("tab", name="PDF", exact=True).click()
    month = whole_month(start, end)
    await page.get_by_role("combobox").click()
    await page.get_by_role(
        "option", name="Month" if month else "Custom", exact=True
    ).click()
    await page.get_by_label("Date", exact=True).click()
    picker = page.get_by_role("dialog").filter(has_text="Select period")
    await _choose_date(picker, start, month=month)
    if not month:
        await _choose_date(picker, end, month=False)
    await picker.get_by_role("button", name="Done", exact=True).click()
    await page.get_by_role("button", name="Get statement", exact=True).click()
