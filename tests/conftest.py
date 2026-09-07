"""Shared fixtures for CashAsh tests."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from db.migrations import apply_migrations
from models import Account, Security
from store import ensure_account, upsert_security


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    """An in-memory database with the full schema applied."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    apply_migrations(connection)
    yield connection
    connection.close()


@pytest.fixture
def account_id(conn: sqlite3.Connection) -> int:
    """A single manual EUR account."""
    return ensure_account(
        conn,
        Account(name="revolut", broker="Revolut", currency="EUR", sync_mode="manual"),
    )


@pytest.fixture
def security_id(conn: sqlite3.Connection) -> int:
    """One USD equity carrying two overlapping themes."""
    return upsert_security(
        conn,
        Security(
            ticker="TEST",
            name="Test Corp",
            currency="USD",
            sector="Information Technology",
            themes=("ai-semis", "mega-cap-tech"),
        ),
    )
