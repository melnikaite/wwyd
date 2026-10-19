# SPDX-License-Identifier: AGPL-3.0-or-later
"""Encryption at rest for backend API keys (Fernet; key in ``<data dir>/secret.key``)."""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from wwyd.config import data_dir

KEY_FILENAME = "secret.key"


def _key_path() -> Path:
    return data_dir() / KEY_FILENAME


def _load_or_create_key() -> bytes:
    path = _key_path()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes().strip()
    key = Fernet.generate_key()
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


def encrypt(plaintext: str) -> str:
    return Fernet(_load_or_create_key()).encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str | None:
    """Plaintext, or None when the key file changed and the value cannot be decrypted."""
    try:
        return Fernet(_load_or_create_key()).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def mask(plaintext: str | None) -> str | None:
    """Show only that a key is set, plus its last 4 characters when it is long enough."""
    if not plaintext:
        return None
    return "****" + plaintext[-4:] if len(plaintext) >= 12 else "****"
