"""Private profile-scoped source archives; no statement data enters the ledger."""

from __future__ import annotations

import hashlib
import json
import os
from io import BytesIO
from pathlib import Path

from profiles import Profile
from revolut_statement import Statement, extract_pdf


def private_path(path: Path) -> Path:
    """Reject repository destinations, resolving symlinks before writing."""
    resolved = path.expanduser().resolve()
    repository = Path(__file__).resolve().parents[1]
    if resolved.is_relative_to(repository):
        raise ValueError(
            "Private Revolut data must live outside the repository under CASH_ASH_HOME."
        )
    return resolved


def revolut_root(profile: Profile) -> Path:
    """Return this person's private Revolut directory."""
    return private_path(profile.root / "revolut")


def _write_once(path: Path, content: bytes) -> None:
    """Create a private immutable artifact; verify an existing one on reruns."""
    path = private_path(path)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_bytes() != content:
            raise ValueError(
                "Existing statement artifact differs; inspect the private archive."
            ) from None
        return
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)


def ingest(profile: Profile, source: Path) -> tuple[Statement, Path]:
    """Validate and archive a PDF and lossless parsed evidence by content hash."""
    content = source.expanduser().read_bytes()
    statement = extract_pdf(BytesIO(content))
    digest = hashlib.sha256(content).hexdigest()
    directory = private_path(revolut_root(profile) / "statements" / digest)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {"format_version": 1, "sha256": digest, "statement": statement.to_dict()}
    _write_once(directory / "statement.pdf", content)
    _write_once(
        directory / "extracted-v1.json", (json.dumps(payload, indent=2) + "\n").encode()
    )
    return statement, directory
