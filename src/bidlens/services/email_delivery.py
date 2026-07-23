from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import requests

from ..config import DAILY_BRIEF_EMAIL_FROM, RESEND_API_KEY


class EmailDeliveryError(RuntimeError):
    """Raised when a transactional email provider rejects or cannot send."""


@dataclass(frozen=True)
class EmailMessage:
    to_email: str
    subject: str
    html_body: str
    text_body: str


@dataclass(frozen=True)
class EmailSendResult:
    provider: str
    message_id: str | None = None
    raw_response: dict[str, Any] | None = None


class EmailSender(Protocol):
    def send(self, message: EmailMessage) -> EmailSendResult:
        ...


class ResendEmailSender:
    provider = "resend"
    endpoint = "https://api.resend.com/emails"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        from_email: str | None = None,
        timeout_seconds: int = 20,
    ) -> None:
        self.api_key = api_key or RESEND_API_KEY
        self.from_email = from_email or DAILY_BRIEF_EMAIL_FROM
        self.timeout_seconds = timeout_seconds

    def send(self, message: EmailMessage) -> EmailSendResult:
        if not self.api_key:
            raise EmailDeliveryError("RESEND_API_KEY is not configured.")
        if not self.from_email:
            raise EmailDeliveryError("DAILY_BRIEF_EMAIL_FROM is not configured.")

        response = requests.post(
            self.endpoint,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": self.from_email,
                "to": [message.to_email],
                "subject": message.subject,
                "html": message.html_body,
                "text": message.text_body,
            },
            timeout=self.timeout_seconds,
        )
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if response.status_code >= 400:
            error_message = payload.get("message") or payload.get("error") or f"HTTP {response.status_code}"
            raise EmailDeliveryError(f"Resend rejected email: {error_message}")
        return EmailSendResult(
            provider=self.provider,
            message_id=str(payload.get("id")) if payload.get("id") else None,
            raw_response=payload,
        )
