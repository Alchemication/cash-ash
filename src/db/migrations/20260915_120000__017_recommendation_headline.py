"""Give a recommendation a one-line headline and a completion condition."""

from __future__ import annotations

import sqlite3

NAME = "recommendation headline and done-when"


def upgrade(conn: sqlite3.Connection) -> None:
    """Add ``headline`` and ``done_when`` to recommendation.

    The rationale was the only text a recommendation carried, so the weekly
    message printed all of it, and the card carrying the buttons printed it
    again. Someone deciding whether anything needs them wants one line first,
    and a review question needs to say what would settle it.

    Nullable because earlier recommendations have neither. Writing one for them
    after the fact would put words in the mouth of a model run that never said
    them; the report falls back to the rationale's first sentence instead.
    """
    conn.executescript(
        """
        ALTER TABLE recommendation ADD COLUMN headline TEXT;
        ALTER TABLE recommendation ADD COLUMN done_when TEXT;
        """
    )
