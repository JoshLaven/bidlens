from __future__ import annotations

import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken

from ..config import SECRET_KEY


def _fernet() -> Fernet:
    digest = hashlib.sha256(SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_credentials(credentials: dict[str, str]) -> str:
    payload = json.dumps(credentials, separators=(",", ":")).encode("utf-8")
    return _fernet().encrypt(payload).decode("ascii")


def decrypt_credentials(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    try:
        payload = _fernet().decrypt(value.encode("ascii"))
        decoded = json.loads(payload.decode("utf-8"))
    except (InvalidToken, ValueError, TypeError, json.JSONDecodeError):
        return {}
    return {
        str(key): str(item)
        for key, item in decoded.items()
        if item is not None
    }
