"""Deterministic normalization and source/actor identity correspondence."""

from __future__ import annotations

from typing import Any

from ..identity_normalization import (
    normalize_display_name, normalize_email, normalized_name_key,
)


def actor_identity_key(actor: Any) -> tuple[str, str] | None:
    if actor.user_id is not None:
        return ("user_id", str(actor.user_id))
    email = normalize_email(actor.email)
    if email:
        return ("email", email)
    name = normalized_name_key(actor.display_name)
    return ("display_name", name) if name else None


def author_identity_key(author: Any) -> tuple[str, str] | None:
    if author is None:
        return None
    if author.user_id is not None:
        return ("user_id", str(author.user_id))
    email = normalize_email(author.address)
    if email:
        return ("email", email)
    name = normalized_name_key(author.display_name)
    return ("display_name", name) if name else None


def actor_matches_author(actor: Any, author: Any) -> bool:
    if author is None:
        return False
    actor_email = normalize_email(actor.email)
    author_email = normalize_email(author.address)
    actor_name = normalize_display_name(actor.display_name)
    author_name = normalize_display_name(author.display_name)
    if actor.user_id is not None:
        return (
            actor.user_id == author.user_id
            and actor_email == author_email
            and actor_name == author_name
        )
    if actor_email is not None:
        return actor_email == author_email and (
            actor_name is None or actor_name == author_name
        )
    return actor_name is not None and normalized_name_key(actor_name) == normalized_name_key(author_name)
