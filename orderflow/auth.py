"""Verify the one HP test-account password without handling its plaintext file."""

from __future__ import annotations

import re
from pathlib import Path

import bcrypt

ACCOUNT = "orderflow"
BCRYPT_HASH = re.compile(rb"\$2[aby]\$(?:0[4-9]|[12][0-9]|3[01])\$[./A-Za-z0-9]{53}\Z")


def load_caddy_hash(path: Path) -> bytes:
    """Read only the bcrypt hash from the exact Caddy credential format we deploy."""
    data = path.read_bytes()
    if len(data) > 256:
        raise ValueError("invalid login credential")
    try:
        lines = [line.strip() for line in data.splitlines() if line.strip()]
        if len(lines) != 3 or lines[0] != b"basic_auth {" or lines[2] != b"}":
            raise ValueError
        account, hashed = lines[1].split()
        if account != ACCOUNT.encode() or not BCRYPT_HASH.fullmatch(hashed):
            raise ValueError
        # Reject a malformed bcrypt salt at startup, before changing the live route.
        bcrypt.checkpw(b"orderflow-credential-check", hashed)
        return hashed
    except (ValueError, TypeError):
        raise ValueError("invalid login credential") from None


def verify_password(password: object, hashed: bytes) -> bool:
    if not isinstance(password, str):
        return False
    encoded = password.encode("utf-8")
    if not 1 <= len(encoded) <= 72 or b"\x00" in encoded:
        return False
    try:
        return bcrypt.checkpw(encoded, hashed)
    except ValueError:
        return False
