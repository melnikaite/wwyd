# SPDX-License-Identifier: AGPL-3.0-or-later
"""Encryption at rest for backend API keys (Fernet).

The key comes from, in order: ``WWYD_SECRET_KEY`` (the Fernet key itself),
``WWYD_SECRET_KEY_FILE`` (a path, ideally outside the data dir), or
``<data dir>/secret.key``, generated on first use. In the last case a backup of the data
dir contains the key together with the values it protects.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from wwyd.config import ConfigError, data_dir

KEY_FILENAME = "secret.key"
ENV_KEY = "WWYD_SECRET_KEY"
ENV_KEY_FILE = "WWYD_SECRET_KEY_FILE"


def _key_path() -> Path:
    configured = os.environ.get(ENV_KEY_FILE, "").strip()
    return Path(configured).expanduser() if configured else data_dir() / KEY_FILENAME


def _validated(key: bytes, source: str) -> bytes:
    key = key.strip()
    try:
        Fernet(key)
    except (ValueError, TypeError):
        raise ConfigError(
            f"the secret key in {source} is empty or not a valid Fernet key; restore it from"
            " a backup (stored API keys cannot be decrypted without it)"
        ) from None
    return key


def _read(path: Path) -> bytes:
    return _validated(path.read_bytes(), str(path))


def _create(path: Path) -> bytes:
    """Write a new key atomically; if another process wins the race, use its key."""
    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".secret.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, path)  # fails if the file exists: never replaces a winner's key
        except FileExistsError:
            return _read(path)
        return key
    finally:
        os.unlink(tmp)


def _load_or_create_key() -> bytes:
    env = os.environ.get(ENV_KEY, "").strip()
    if env:
        return _validated(env.encode(), ENV_KEY)
    path = _key_path()
    try:
        return _read(path)
    except FileNotFoundError:
        if os.environ.get(ENV_KEY_FILE, "").strip():
            raise ConfigError(f"{ENV_KEY_FILE} points at a missing file") from None
        return _create(path)


def check_key() -> None:
    """Startup check: raise :class:`ConfigError` if a configured or existing key is unusable
    (a missing data-dir key is fine; it is generated on first use)."""
    env = os.environ.get(ENV_KEY, "").strip()
    if env:
        _validated(env.encode(), ENV_KEY)
        return
    path = _key_path()
    if path.exists() or os.environ.get(ENV_KEY_FILE, "").strip():
        try:
            _read(path)
        except FileNotFoundError:
            raise ConfigError(f"{ENV_KEY_FILE} points at a missing file") from None


def encrypt(plaintext: str) -> str:
    return Fernet(_load_or_create_key()).encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str | None:
    """Plaintext, or None when the key changed and the value cannot be decrypted."""
    fernet = Fernet(_load_or_create_key())
    try:
        return fernet.decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def mask(plaintext: str | None) -> str | None:
    """Show only that a key is set, plus its last 4 characters when it is long enough."""
    if not plaintext:
        return None
    return "****" + plaintext[-4:] if len(plaintext) >= 12 else "****"
