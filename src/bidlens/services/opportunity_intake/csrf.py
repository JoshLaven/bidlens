from __future__ import annotations

from itsdangerous import BadSignature, URLSafeTimedSerializer

from ... import config


def intake_csrf_token(user_id: int, *, action: str, draft_id: int | None = None) -> str:
    return URLSafeTimedSerializer(config.SECRET_KEY, salt="opportunity-intake").dumps({
        "user_id": user_id,
        "action": action,
        "draft_id": draft_id,
    })


def validate_intake_csrf_token(
    token: str,
    user_id: int,
    *,
    action: str,
    draft_id: int | None = None,
) -> bool:
    try:
        data = URLSafeTimedSerializer(config.SECRET_KEY, salt="opportunity-intake").loads(
            token, max_age=3600
        )
    except BadSignature:
        return False
    return data == {"user_id": user_id, "action": action, "draft_id": draft_id}
