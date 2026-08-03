"""Neutral normalization for stored organizational evidence identities."""

from __future__ import annotations

import re
import unicodedata


_SPACE = re.compile(r"\s+")
PLACEHOLDER_NAMES = frozenset({"unknown sender", "unknown user"})


def normalize_display_name(value: str | None) -> str | None:
    normalized = _SPACE.sub(" ", unicodedata.normalize("NFKC", value or "")).strip()
    if not normalized or normalized.casefold() in PLACEHOLDER_NAMES or re.fullmatch(
        r"user\s+\d+", normalized, re.I,
    ):
        return None
    if len(normalized) > 200:
        raise ValueError("display_name exceeds 200 characters")
    return normalized


def normalize_email(value: str | None) -> str | None:
    normalized = unicodedata.normalize("NFKC", value or "").strip().casefold()
    if not normalized:
        return None
    if len(normalized) > 320:
        raise ValueError("email exceeds 320 characters")
    return normalized


def normalized_name_key(value: str | None) -> str | None:
    normalized = normalize_display_name(value)
    return normalized.casefold() if normalized else None
