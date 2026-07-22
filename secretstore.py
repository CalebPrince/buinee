"""Encryption for credentials that have to be kept, not hashed.

Passwords are hashed (db._hash_password) because nothing ever needs the
original back. Mailbox credentials are the opposite: an IMAP password and an
OAuth refresh token both have to be replayed to a third party months later,
so they must be recoverable - which means encrypted at rest rather than
hashed.

What this protects against: someone who ends up with a copy of
`storage/ledgerline.db` - a stray backup, a misconfigured File Manager, a
support ticket with the wrong attachment - and no copy of the key. It does
not protect against someone who has the server, since the key is on it. That
is the honest limit of any key-on-the-same-box scheme, and it's still worth
having, because database files travel far more easily than servers do.

Fail-closed by design: with no key configured, connecting a mailbox is
refused outright. Silently falling back to plaintext would be the one
behaviour nobody would notice and everybody would regret.
"""

from __future__ import annotations

import json

try:
    from cryptography.fernet import Fernet, InvalidToken
    HAVE_CRYPTO = True
except ImportError:  # not installed in this virtualenv yet
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[misc,assignment]
    HAVE_CRYPTO = False

KEY_NAME = "BUINEE_SECRET_KEY"


class SecretsUnavailable(Exception):
    """No usable key - callers should refuse to store credentials."""


def generate_key() -> str:
    """A fresh key, for `python -c` during setup. Not called at runtime."""
    if not HAVE_CRYPTO:
        raise SecretsUnavailable("The cryptography package isn't installed.")
    return Fernet.generate_key().decode()


def why_unavailable(cfg: dict) -> str:
    """Empty string when usable, otherwise the reason, for the UI to show."""
    if not HAVE_CRYPTO:
        return ("The cryptography package isn't installed in this "
                "environment - run: pip install cryptography")
    if not cfg.get(KEY_NAME, "").strip():
        return (f"{KEY_NAME} isn't set, so mailbox credentials can't be "
                "stored safely.")
    try:
        Fernet(cfg[KEY_NAME].strip().encode())
    except Exception:
        return f"{KEY_NAME} isn't a valid key - generate a new one."
    return ""


def is_ready(cfg: dict) -> bool:
    return not why_unavailable(cfg)


def _box(cfg: dict) -> "Fernet":
    reason = why_unavailable(cfg)
    if reason:
        raise SecretsUnavailable(reason)
    return Fernet(cfg[KEY_NAME].strip().encode())


def encrypt(cfg: dict, payload: dict) -> str:
    """Encrypt a dict of credentials into a single storable string."""
    return _box(cfg).encrypt(json.dumps(payload).encode()).decode()


def decrypt(cfg: dict, blob: str) -> dict:
    """Reverse of encrypt.

    A blob that won't decrypt means the key changed (or the row was written
    under a different one). That's unrecoverable for that row, so it reads as
    a broken connection the person can simply reconnect, not as a crash.
    """
    try:
        return json.loads(_box(cfg).decrypt(blob.encode()).decode())
    except SecretsUnavailable:
        raise
    except (InvalidToken, ValueError, TypeError) as exc:
        raise SecretsUnavailable(
            "Stored credentials couldn't be read with the current "
            f"{KEY_NAME}. Reconnect the mailbox."
        ) from exc
