from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import requests
from sqlalchemy.orm import Session

from .. import config
from ..models import Event, ExternalIntegrationConnection, User, Workspace
from .integration_credentials import decrypt_credentials, encrypt_credentials


PROVIDER_MICROSOFT = "microsoft"
ALLOWED_PROVIDERS = {PROVIDER_MICROSOFT}
STATUS_CONNECTED = "connected"
STATUS_REAUTHORIZATION_REQUIRED = "reauthorization_required"
STATUS_CONNECTION_ERROR = "connection_error"
STATUS_DISCONNECTED = "disconnected"
PERSISTED_CONNECTION_STATUSES = {
    STATUS_CONNECTED,
    STATUS_REAUTHORIZATION_REQUIRED,
    STATUS_CONNECTION_ERROR,
    STATUS_DISCONNECTED,
}
MICROSOFT_SCOPES = (
    "openid",
    "profile",
    "email",
    "offline_access",
    "User.Read",
    "Mail.Send",
    "Mail.ReadWrite",
)
MICROSOFT_GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me?$select=id,displayName,userPrincipalName,mail"
MICROSOFT_SEND_MAIL_URL = "https://graph.microsoft.com/v1.0/me/sendMail"
MICROSOFT_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"
MICROSOFT_IMMUTABLE_ID_HEADER = 'IdType="ImmutableId"'
MICROSOFT_TRACKING_SELECT = (
    "id,conversationId,internetMessageId,sender,from,toRecipients,ccRecipients,"
    "subject,body,sentDateTime,webLink"
)
MICROSOFT_SYNC_SELECT = MICROSOFT_TRACKING_SELECT + ",receivedDateTime,isDraft"
TOKEN_EXPIRY_SKEW_SECONDS = 300


class MicrosoftConfigError(RuntimeError):
    pass


class MicrosoftConnectionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class MicrosoftIdentity:
    tenant_id: str | None
    user_id: str
    email: str | None
    display_name: str | None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_provider(provider: str) -> str:
    normalized = (provider or "").strip().lower()
    if normalized not in ALLOWED_PROVIDERS:
        raise MicrosoftConnectionError("unsupported_provider", "Unsupported integration provider.")
    return normalized


def generate_pkce_pair() -> tuple[str, str]:
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return code_verifier, code_challenge


def state_digest(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def safe_error_message(code: str) -> str:
    return {
        "configuration_error": "Microsoft connection settings are not configured.",
        "consent_denied": "Microsoft authorization was not completed.",
        "invalid_state": "Microsoft authorization could not be verified. Please try again.",
        "expired_oauth_attempt": "Microsoft authorization expired. Please start again.",
        "token_exchange_failed": "Microsoft authorization could not be completed.",
        "invalid_grant": "Microsoft authorization has expired or was revoked.",
        "identity_verification_failed": "BidLens could not verify the connected Microsoft identity.",
        "identity_mismatch": "The Microsoft identity no longer matches this connection. Please reconnect.",
        "provider_unavailable": "Microsoft is temporarily unavailable.",
        "permission_missing": "Required Microsoft mail permissions are missing. Reconnect your account to approve them.",
        "invalid_recipient": "Microsoft rejected one or more recipients.",
        "invalid_form": "Review the recipients, subject, and message before sending.",
        "message_rejected": "Microsoft rejected the email request.",
        "provider_throttled": "Microsoft is throttling email requests. Please try again later.",
        "outcome_uncertain": "BidLens could not confirm whether Microsoft accepted this email. Check your Sent Items before trying again to avoid sending a duplicate.",
        "not_connected": "No Microsoft account is connected.",
    }.get(code, "Microsoft connection could not be completed.")


def safe_return_path(path: str | None, *, default: str = "/integrations/microsoft") -> str:
    value = (path or "").strip()
    if not value.startswith("/") or value.startswith("//"):
        return default
    if any(value.startswith(prefix) for prefix in ("/integrations/microsoft", "/my-settings", "/integrations", "/opportunity/")):
        return value
    return default


def _decode_jwt_payload_unverified(token: str | None) -> dict[str, Any]:
    if not token or token.count(".") < 2:
        return {}
    try:
        payload_segment = token.split(".", 2)[1]
        padded = payload_segment + "=" * (-len(payload_segment) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return {}


def connection_status_label(status: str | None) -> tuple[str, str]:
    if status == STATUS_CONNECTED:
        return "Connected", "success"
    if status == STATUS_REAUTHORIZATION_REQUIRED:
        return "Reauthorization required", "warning"
    if status == STATUS_CONNECTION_ERROR:
        return "Connection error", "warning"
    if status == STATUS_DISCONNECTED:
        return "Disconnected", "neutral"
    return "Not connected", "neutral"


def connection_status_summary(connection: ExternalIntegrationConnection | None) -> dict[str, Any]:
    status = connection.connection_status if connection else "not_connected"
    label, level = connection_status_label(status)
    return {
        "status": status,
        "label": label,
        "level": level,
        "connected": bool(connection and status == STATUS_CONNECTED),
        "connected_email": connection.connected_email if connection else None,
        "connected_display_name": connection.connected_display_name if connection else None,
        "connected_at": connection.connected_at if connection else None,
        "last_verified_at": connection.last_verified_at if connection else None,
        "last_error_code": connection.last_error_code if connection else None,
        "last_error_message": safe_error_message(connection.last_error_code) if connection and connection.last_error_code else None,
        "disconnected_at": connection.disconnected_at if connection else None,
        "has_mail_send": connection_has_scope(connection, "Mail.Send") if connection else False,
        "has_mail_read_write": connection_has_scope(connection, "Mail.ReadWrite") if connection else False,
    }


def parse_scopes(value: str | None) -> set[str]:
    return {scope.strip().lower() for scope in str(value or "").replace(",", " ").split() if scope.strip()}


def connection_has_scope(connection: ExternalIntegrationConnection | None, scope: str) -> bool:
    if not connection:
        return False
    return scope.strip().lower() in parse_scopes(connection.granted_scopes)


class MicrosoftConnectionService:
    def __init__(self, *, db: Session, workspace: Workspace, user: User) -> None:
        self.db = db
        self.workspace = workspace
        self.user = user

    @property
    def authority_base(self) -> str:
        tenant = (config.MICROSOFT_TENANT_ID or "common").strip().strip("/")
        return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"

    def _validate_config(self) -> None:
        missing = [
            name
            for name, value in [
                ("MICROSOFT_CLIENT_ID", config.MICROSOFT_CLIENT_ID),
                ("MICROSOFT_CLIENT_SECRET", config.MICROSOFT_CLIENT_SECRET),
                ("MICROSOFT_REDIRECT_URI", config.MICROSOFT_REDIRECT_URI),
            ]
            if not (value or "").strip()
        ]
        if missing:
            raise MicrosoftConfigError(f"Missing Microsoft config: {', '.join(missing)}")

    def connection(self) -> ExternalIntegrationConnection | None:
        return (
            self.db.query(ExternalIntegrationConnection)
            .filter(
                ExternalIntegrationConnection.workspace_id == self.workspace.id,
                ExternalIntegrationConnection.user_id == self.user.id,
                ExternalIntegrationConnection.provider == PROVIDER_MICROSOFT,
            )
            .first()
        )

    def safe_status(self) -> dict[str, Any]:
        return connection_status_summary(self.connection())

    def build_authorization_url(self, *, state: str, code_challenge: str) -> str:
        self._validate_config()
        params = {
            "client_id": config.MICROSOFT_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": config.MICROSOFT_REDIRECT_URI,
            "response_mode": "query",
            "scope": " ".join(MICROSOFT_SCOPES),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        return f"{self.authority_base}/authorize?{urlencode(params)}"

    def exchange_authorization_code(self, *, code: str, code_verifier: str) -> dict[str, Any]:
        self._validate_config()
        response = requests.post(
            f"{self.authority_base}/token",
            data={
                "client_id": config.MICROSOFT_CLIENT_ID,
                "client_secret": config.MICROSOFT_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config.MICROSOFT_REDIRECT_URI,
                "code_verifier": code_verifier,
            },
            timeout=20,
        )
        if not response.ok:
            raise MicrosoftConnectionError("token_exchange_failed", safe_error_message("token_exchange_failed"))
        return response.json()

    def _headers(self, access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

    def verify_identity(self, access_token: str, *, id_token: str | None = None) -> MicrosoftIdentity:
        response = requests.get(MICROSOFT_GRAPH_ME_URL, headers=self._headers(access_token), timeout=20)
        if not response.ok:
            raise MicrosoftConnectionError("identity_verification_failed", safe_error_message("identity_verification_failed"))
        data = response.json()
        user_id = str(data.get("id") or "").strip()
        if not user_id:
            raise MicrosoftConnectionError("identity_verification_failed", safe_error_message("identity_verification_failed"))
        email = str(data.get("mail") or data.get("userPrincipalName") or "").strip() or None
        display_name = str(data.get("displayName") or "").strip() or None
        id_claims = _decode_jwt_payload_unverified(id_token)
        claim_user_id = str(id_claims.get("oid") or id_claims.get("sub") or "").strip()
        if claim_user_id and claim_user_id != user_id:
            raise MicrosoftConnectionError("identity_mismatch", safe_error_message("identity_mismatch"))
        tenant_id = str(id_claims.get("tid") or data.get("tenantId") or data.get("tid") or "").strip() or None
        return MicrosoftIdentity(tenant_id=tenant_id, user_id=user_id, email=email, display_name=display_name)

    def _audit(self, event_type: str, *, outcome: str, error_code: str | None = None) -> None:
        self.db.add(Event(
            org_id=self.workspace.organization_id,
            user_id=self.user.id,
            event_type=event_type,
            payload={
                "provider": PROVIDER_MICROSOFT,
                "workspace_id": self.workspace.id,
                "outcome": outcome,
                "error_code": error_code,
            },
        ))

    def _record_error(self, connection: ExternalIntegrationConnection | None, code: str) -> None:
        if connection:
            connection.connection_status = (
                STATUS_REAUTHORIZATION_REQUIRED
                if code in {"invalid_grant", "identity_mismatch"}
                else STATUS_CONNECTION_ERROR
            )
            connection.last_error_at = utcnow()
            connection.last_error_code = code
            connection.last_error_message = safe_error_message(code)

    def complete_connection(self, token_response: dict[str, Any]) -> ExternalIntegrationConnection:
        access_token = token_response.get("access_token")
        refresh_token = token_response.get("refresh_token")
        if not access_token or not refresh_token:
            raise MicrosoftConnectionError("token_exchange_failed", safe_error_message("token_exchange_failed"))
        identity = self.verify_identity(str(access_token), id_token=token_response.get("id_token"))

        duplicate = (
            self.db.query(ExternalIntegrationConnection)
            .filter(
                ExternalIntegrationConnection.workspace_id == self.workspace.id,
                ExternalIntegrationConnection.provider == PROVIDER_MICROSOFT,
                ExternalIntegrationConnection.external_user_id == identity.user_id,
                ExternalIntegrationConnection.user_id != self.user.id,
            )
            .first()
        )
        if duplicate:
            self._audit("integration_lifecycle", outcome="failed", error_code="identity_already_connected")
            raise MicrosoftConnectionError("identity_mismatch", "This Microsoft identity is already connected to another BidLens user.")

        connection = self.connection()
        now = utcnow()
        if connection is None:
            connection = ExternalIntegrationConnection(
                workspace_id=self.workspace.id,
                user_id=self.user.id,
                provider=PROVIDER_MICROSOFT,
                connected_at=now,
            )
            self.db.add(connection)
        connection.connection_status = STATUS_CONNECTED
        connection.external_tenant_id = identity.tenant_id
        connection.external_user_id = identity.user_id
        connection.connected_email = identity.email
        connection.connected_display_name = identity.display_name
        connection.encrypted_access_token = encrypt_credentials({"token": str(access_token)})
        connection.encrypted_refresh_token = encrypt_credentials({"token": str(refresh_token)})
        connection.access_token_expires_at = now + timedelta(seconds=int(token_response.get("expires_in") or 3600))
        connection.granted_scopes = " ".join(str(token_response.get("scope") or " ".join(MICROSOFT_SCOPES)).split())
        connection.connected_at = connection.connected_at or now
        connection.last_refreshed_at = now
        connection.last_verified_at = now
        connection.last_error_at = None
        connection.last_error_code = None
        connection.last_error_message = None
        connection.disconnected_at = None
        self._audit("integration_lifecycle", outcome="connected")
        self.db.flush()
        return connection

    def _decrypt_token(self, connection: ExternalIntegrationConnection, kind: str) -> str | None:
        encrypted = getattr(connection, f"encrypted_{kind}_token")
        return decrypt_credentials(encrypted).get("token")

    def refresh_access_token(self, connection: ExternalIntegrationConnection) -> str:
        refresh_token = self._decrypt_token(connection, "refresh")
        if not refresh_token:
            self._record_error(connection, "invalid_grant")
            self._audit("integration_lifecycle", outcome="reauthorization_required", error_code="invalid_grant")
            raise MicrosoftConnectionError("invalid_grant", safe_error_message("invalid_grant"))
        self._validate_config()
        response = requests.post(
            f"{self.authority_base}/token",
            data={
                "client_id": config.MICROSOFT_CLIENT_ID,
                "client_secret": config.MICROSOFT_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": " ".join(MICROSOFT_SCOPES),
            },
            timeout=20,
        )
        if not response.ok:
            code = "invalid_grant" if response.status_code in {400, 401} else "provider_unavailable"
            self._record_error(connection, code)
            self._audit("integration_lifecycle", outcome="refresh_failed", error_code=code)
            raise MicrosoftConnectionError(code, safe_error_message(code))
        data = response.json()
        access_token = data.get("access_token")
        if not access_token:
            self._record_error(connection, "token_exchange_failed")
            raise MicrosoftConnectionError("token_exchange_failed", safe_error_message("token_exchange_failed"))
        now = utcnow()
        connection.encrypted_access_token = encrypt_credentials({"token": str(access_token)})
        if data.get("refresh_token"):
            connection.encrypted_refresh_token = encrypt_credentials({"token": str(data["refresh_token"])})
        connection.access_token_expires_at = now + timedelta(seconds=int(data.get("expires_in") or 3600))
        connection.connection_status = STATUS_CONNECTED
        connection.last_refreshed_at = now
        connection.last_error_at = None
        connection.last_error_code = None
        connection.last_error_message = None
        self._audit("integration_lifecycle", outcome="token_refreshed")
        self.db.flush()
        return str(access_token)

    def access_token_for_connection(self, connection: ExternalIntegrationConnection) -> str:
        if connection.workspace_id != self.workspace.id or connection.user_id != self.user.id:
            raise MicrosoftConnectionError("not_connected", "Connection does not belong to this user.")
        expires_at = connection.access_token_expires_at
        now = utcnow()
        if expires_at is not None:
            comparable = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
            if comparable <= now + timedelta(seconds=TOKEN_EXPIRY_SKEW_SECONDS):
                return self.refresh_access_token(connection)
        access_token = self._decrypt_token(connection, "access")
        if not access_token:
            return self.refresh_access_token(connection)
        return access_token

    def test_connection(self) -> MicrosoftIdentity:
        connection = self.connection()
        if not connection or connection.connection_status == STATUS_DISCONNECTED:
            raise MicrosoftConnectionError("not_connected", safe_error_message("not_connected"))
        token = self.access_token_for_connection(connection)
        identity = self.verify_identity(token)
        if identity.user_id != connection.external_user_id or (
            connection.external_tenant_id and identity.tenant_id and identity.tenant_id != connection.external_tenant_id
        ):
            self._record_error(connection, "identity_mismatch")
            self._audit("integration_lifecycle", outcome="identity_mismatch", error_code="identity_mismatch")
            raise MicrosoftConnectionError("identity_mismatch", safe_error_message("identity_mismatch"))
        connection.last_verified_at = utcnow()
        connection.connection_status = STATUS_CONNECTED
        connection.last_error_at = None
        connection.last_error_code = None
        connection.last_error_message = None
        self._audit("integration_lifecycle", outcome="tested")
        self.db.flush()
        return identity

    def send_mail_for_current_user(
        self,
        *,
        to_recipients: list[str],
        subject: str,
        body_text: str,
    ) -> dict[str, str]:
        connection = self.connection()
        if not connection or connection.connection_status == STATUS_DISCONNECTED:
            raise MicrosoftConnectionError("not_connected", safe_error_message("not_connected"))
        if connection.connection_status == STATUS_REAUTHORIZATION_REQUIRED:
            raise MicrosoftConnectionError("reauthorization_required", safe_error_message("invalid_grant"))
        if not connection_has_scope(connection, "Mail.Send"):
            raise MicrosoftConnectionError("permission_missing", safe_error_message("permission_missing"))

        token = self.access_token_for_connection(connection)
        identity = self.verify_identity(token)
        if identity.user_id != connection.external_user_id or (
            connection.external_tenant_id and identity.tenant_id and identity.tenant_id != connection.external_tenant_id
        ):
            self._record_error(connection, "identity_mismatch")
            self._audit("integration_lifecycle", outcome="identity_mismatch", error_code="identity_mismatch")
            raise MicrosoftConnectionError("identity_mismatch", safe_error_message("identity_mismatch"))

        if not connection_has_scope(connection, "Mail.ReadWrite"):
            raise MicrosoftConnectionError("permission_missing", safe_error_message("permission_missing"))

        message_payload = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body_text},
            "toRecipients": [
                {"emailAddress": {"address": address}}
                for address in to_recipients
            ],
        }
        immutable_headers = {
            **self._headers(token),
            "Content-Type": "application/json",
            "Prefer": MICROSOFT_IMMUTABLE_ID_HEADER,
        }
        try:
            draft_response = requests.post(
                MICROSOFT_MESSAGES_URL,
                headers=immutable_headers,
                json=message_payload,
                timeout=20,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise MicrosoftConnectionError("provider_unavailable", safe_error_message("provider_unavailable")) from exc

        if draft_response.status_code != 201:
            self._raise_send_error(draft_response, connection=connection)
        try:
            draft = draft_response.json()
        except ValueError as exc:
            raise MicrosoftConnectionError("provider_unavailable", safe_error_message("provider_unavailable")) from exc
        immutable_message_id = str(draft.get("id") or "").strip()
        if not immutable_message_id:
            raise MicrosoftConnectionError("provider_unavailable", safe_error_message("provider_unavailable"))

        send_url = f"{MICROSOFT_MESSAGES_URL}/{quote(immutable_message_id, safe='')}/send"
        try:
            response = requests.post(send_url, headers=immutable_headers, timeout=20)
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise MicrosoftConnectionError("outcome_uncertain", safe_error_message("outcome_uncertain")) from exc

        if response.status_code == 202:
            self._audit("integration_lifecycle", outcome="mail_send_accepted")
            sent_message = self._retrieve_sent_message(
                token=token,
                immutable_message_id=immutable_message_id,
            )
            tracking_error = None
            if sent_message is None:
                tracking_error = "metadata_retrieval_failed"
                self._audit(
                    "integration_lifecycle",
                    outcome="mail_tracking_metadata_failed",
                    error_code=tracking_error,
                )
            metadata = dict(draft)
            metadata.update(
                {
                    key: value
                    for key, value in (sent_message or {}).items()
                    if value not in (None, "")
                }
            )
            self.db.flush()
            return {
                "status": "accepted_for_delivery",
                "provider": PROVIDER_MICROSOFT,
                "provider_mailbox_id": connection.external_user_id,
                "tracking_error": tracking_error,
                "message": metadata,
            }
        self._raise_send_error(response, connection=connection)
        raise AssertionError("unreachable")

    def _retrieve_sent_message(
        self,
        *,
        token: str,
        immutable_message_id: str,
    ) -> dict[str, Any] | None:
        url = (
            f"{MICROSOFT_MESSAGES_URL}/{quote(immutable_message_id, safe='')}"
            f"?$select={MICROSOFT_TRACKING_SELECT}"
        )
        headers = {
            **self._headers(token),
            "Prefer": MICROSOFT_IMMUTABLE_ID_HEADER,
        }
        for attempt in range(3):
            try:
                response = requests.get(url, headers=headers, timeout=20)
            except (requests.Timeout, requests.ConnectionError):
                response = None
            if response is not None and response.status_code == 200:
                try:
                    return response.json()
                except ValueError:
                    return None
            if attempt < 2:
                time.sleep(0.25 * (attempt + 1))
        return None

    def list_conversation_messages(self, provider_conversation_id: str) -> list[dict[str, Any]]:
        """List only one tracked Graph conversation, preserving immutable message IDs."""
        conversation_id = str(provider_conversation_id or "").strip()
        if not conversation_id:
            raise MicrosoftConnectionError("invalid_conversation", "Tracked conversation is unavailable.")
        connection = self.connection()
        if not connection or connection.connection_status == STATUS_DISCONNECTED:
            raise MicrosoftConnectionError("not_connected", safe_error_message("not_connected"))
        if connection.connection_status == STATUS_REAUTHORIZATION_REQUIRED:
            raise MicrosoftConnectionError("reauthorization_required", safe_error_message("invalid_grant"))
        if not connection_has_scope(connection, "Mail.ReadWrite"):
            raise MicrosoftConnectionError("permission_missing", safe_error_message("permission_missing"))

        token = self.access_token_for_connection(connection)
        escaped_id = conversation_id.replace("'", "''")
        url = MICROSOFT_MESSAGES_URL
        params: dict[str, Any] | None = {
            "$filter": f"conversationId eq '{escaped_id}'",
            "$select": MICROSOFT_SYNC_SELECT,
            "$top": 50,
        }
        headers = {**self._headers(token), "Prefer": MICROSOFT_IMMUTABLE_ID_HEADER}
        messages: list[dict[str, Any]] = []
        while url:
            try:
                response = requests.get(url, params=params, headers=headers, timeout=20)
            except (requests.Timeout, requests.ConnectionError) as exc:
                raise MicrosoftConnectionError("provider_unavailable", safe_error_message("provider_unavailable")) from exc
            if response.status_code in {401, 403}:
                self._record_error(connection, "invalid_grant")
                raise MicrosoftConnectionError("reauthorization_required", safe_error_message("invalid_grant"))
            if response.status_code == 429:
                raise MicrosoftConnectionError("provider_throttled", safe_error_message("provider_throttled"))
            if response.status_code != 200:
                raise MicrosoftConnectionError("provider_unavailable", safe_error_message("provider_unavailable"))
            try:
                payload = response.json()
            except ValueError as exc:
                raise MicrosoftConnectionError("provider_unavailable", safe_error_message("provider_unavailable")) from exc
            values = payload.get("value")
            if not isinstance(values, list):
                raise MicrosoftConnectionError("provider_unavailable", safe_error_message("provider_unavailable"))
            messages.extend(item for item in values if isinstance(item, dict))
            next_link = str(payload.get("@odata.nextLink") or "").strip()
            if next_link:
                parsed = urlparse(next_link)
                if parsed.scheme != "https" or parsed.netloc != "graph.microsoft.com" or not parsed.path.startswith("/v1.0/me/messages"):
                    raise MicrosoftConnectionError("provider_unavailable", safe_error_message("provider_unavailable"))
            url = next_link
            params = None  # nextLink contains the complete provider-generated query.
        return messages

    def _raise_send_error(
        self,
        response: requests.Response,
        *,
        connection: ExternalIntegrationConnection,
    ) -> None:
        if response.status_code in {401, 403}:
            self._record_error(connection, "invalid_grant")
            raise MicrosoftConnectionError("reauthorization_required", safe_error_message("invalid_grant"))
        if response.status_code == 400:
            raise MicrosoftConnectionError("invalid_recipient", safe_error_message("invalid_recipient"))
        if response.status_code == 429:
            raise MicrosoftConnectionError("provider_throttled", safe_error_message("provider_throttled"))
        if 500 <= response.status_code:
            raise MicrosoftConnectionError("provider_unavailable", safe_error_message("provider_unavailable"))
        raise MicrosoftConnectionError("message_rejected", safe_error_message("message_rejected"))

    def disconnect(self) -> None:
        connection = self.connection()
        if not connection:
            return
        connection.encrypted_access_token = None
        connection.encrypted_refresh_token = None
        connection.access_token_expires_at = None
        connection.connection_status = STATUS_DISCONNECTED
        connection.disconnected_at = utcnow()
        connection.last_error_at = None
        connection.last_error_code = None
        connection.last_error_message = None
        self._audit("integration_lifecycle", outcome="disconnected")
        self.db.flush()
