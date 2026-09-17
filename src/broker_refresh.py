"""One broker refresh: relay sign-in, download a statement, reconcile it.

This is the broker half of a refresh — statement acquisition and comparison,
which can need phone approval. Market-price/FX refresh is the separate ``sync``
command and does not touch Revolut. Nothing here writes trades, cash flows or
prices; a refresh only archives evidence and compares quantities, exactly as
``revolut ingest`` does, so a failed or half-finished run leaves the ledger
untouched.

The flow returns a :class:`RefreshResult` rather than sending anything itself,
so the daemon owns every Telegram message and this stays testable with fakes.
The QR link is handed out through ``deliver_link`` — the daemon sends it as a
reply to the owner's own "Run now", never unsolicited.

Public API:
    RefreshResult   -- what a refresh produced
    run_broker_refresh -- drive one refresh end to end
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date

from profiles import Profile
from revolut_login import relay_sign_in

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RefreshResult:
    """The outcome of one broker refresh.

    Attributes:
        status: ``reconciled`` when a statement was downloaded and compared;
            ``sign_in_needed`` when the owner must sign in at the Mac (the QR
            could not be relayed or the flow hit an unexpected screen);
            ``not_approved`` when the relayed link was not approved in time;
            ``failed`` for a download or parse error.
        detail: A short line safe to show and log; never the login token.
        reconciliation: The comparison from ``revolut_reconcile.reconcile`` when
            ``reconciled``, else None.
        archive: The statement's private archive directory when ``reconciled``.
    """

    status: str
    detail: str
    reconciliation: dict | None = None
    archive: str | None = None

    @property
    def ok(self) -> bool:
        """True only when a statement was downloaded and reconciled."""
        return self.status == "reconciled"


async def run_broker_refresh(
    profile: Profile,
    *,
    deliver_link: Callable[[str], Awaitable[None]],
    on_signed_in: Callable[[], Awaitable[None]] | None = None,
    start: date | None = None,
    end: date | None = None,
    download=None,  # type: ignore[no-untyped-def]
    reconcile_fn=None,  # type: ignore[no-untyped-def]
    import_fn=None,  # type: ignore[no-untyped-def]
) -> RefreshResult:
    """Sign in by QR relay, download the period's statement, reconcile it.

    Args:
        profile: Whose account to refresh.
        deliver_link: Async callback given the login QR link, once, to relay to
            the owner's phone.
        on_signed_in: Async callback run when the portfolio is reached, before
            the download begins — the "signed in" confirmation that an approval
            was expected. A missing confirmation is the owner's cue that someone
            else's login was approved.
        start: First day of the period; defaults to the current calendar month.
        end: Last day of the period.
        download: Injection point for the downloader, for tests. Defaults to
            :func:`revolut_download.download_statement`.
        reconcile_fn: Injection point for reconciliation, for tests. Defaults to
            :func:`revolut_reconcile.reconcile`.
        import_fn: Injection point for the ledger import, for tests. Defaults to
            :func:`revolut_fills.import_activity`.

    Returns:
        The result. Only ``reconciled`` touched the archive; every other status
        left nothing behind.
    """
    if download is None:
        from revolut_download import download_statement

        download = download_statement
    if reconcile_fn is None:
        from revolut_reconcile import reconcile as reconcile_fn
    if import_fn is None:
        from revolut_fills import import_activity as import_fn

    from revolut_download import SignInRequired
    from store import open_existing_db

    async def sign_in(page):  # type: ignore[no-untyped-def]
        outcome = await relay_sign_in(page, deliver=deliver_link)
        if outcome.ok and on_signed_in is not None:
            await on_signed_in()
        return outcome

    try:
        statement, archive = await download(
            profile, start=start, end=end, sign_in=sign_in
        )
    except SignInRequired as exc:
        status = "not_approved" if exc.outcome.status == "timeout" else "sign_in_needed"
        return RefreshResult(status, exc.outcome.detail)
    except ValueError as exc:
        logger.warning("Broker refresh could not download a statement: %s", exc)
        return RefreshResult("failed", str(exc))

    try:
        with open_existing_db(profile.db) as conn:
            reconciliation = reconcile_fn(conn, statement)
            try:
                imported = import_fn(conn, statement).summary()
            except Exception as exc:  # noqa: BLE001 - the statement is archived
                # The download and the comparison already succeeded; a failed
                # import is worth saying, not worth discarding them for.
                logger.exception("Could not import statement activity")
                imported = f"Ledger import failed ({exc})."
    except Exception as exc:  # noqa: BLE001 - the statement is archived; report the miss
        logger.exception("Reconciliation failed after a successful download")
        return RefreshResult(
            "failed", f"Downloaded a statement but could not reconcile it ({exc})."
        )

    differing = sum(row["status"] != "quantity_match" for row in reconciliation["rows"])
    detail = (
        f"Statement {statement.period_start}–{statement.period_end} archived; "
        f"{differing} of {len(reconciliation['rows'])} rows differ from the ledger. "
        f"{imported}"
    )
    return RefreshResult(
        "reconciled", detail, reconciliation=reconciliation, archive=str(archive)
    )
