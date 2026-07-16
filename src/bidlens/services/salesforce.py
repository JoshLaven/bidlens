from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from typing import Any

import requests
from sqlalchemy.orm import Session

from .. import config
from ..models import SalesforceConnection
from .integration_credentials import decrypt_credentials, encrypt_credentials


SALESFORCE_API_VERSION = "v60.0"
PROSPECT_FEED_STATUS = "Prospect_Feed"
SALESFORCE_OAUTH_SCOPES = "api refresh_token"
_TOKEN_CACHE: dict[str, str] = {}
CONNECTION_STATUSES = {
    "not_connected", "connected", "reauthorization_required", "connection_error",
}


class SalesforceConfigError(RuntimeError):
    pass


class SalesforceApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class SalesforceOpportunity:
    id: str
    name: str | None
    external_source_id: str | None
    intake_status: str | None


def generate_pkce_pair() -> tuple[str, str]:
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return code_verifier, code_challenge


class SalesforceService:
    def __init__(
        self,
        *,
        instance_url: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        db: Session | None = None,
        workspace_id: int | None = None,
        connection: SalesforceConnection | None = None,
    ) -> None:
        self.instance_url = (instance_url or config.SALESFORCE_INSTANCE_URL or "").rstrip("/")
        self.client_id = client_id or config.SALESFORCE_CLIENT_ID
        self.client_secret = client_secret or config.SALESFORCE_CLIENT_SECRET
        self.db = db
        self.workspace_id = workspace_id
        self.connection = connection
        if self.connection is None and db is not None and workspace_id is not None:
            self.connection = db.query(SalesforceConnection).filter(
                SalesforceConnection.workspace_id == workspace_id
            ).first()

    def _validate_config(self) -> None:
        missing = [
            name
            for name, value in [
                ("SALESFORCE_INSTANCE_URL", self.instance_url),
                ("SALESFORCE_CLIENT_ID", self.client_id),
                ("SALESFORCE_CLIENT_SECRET", self.client_secret),
            ]
            if not value
        ]
        if missing:
            raise SalesforceConfigError(f"Missing Salesforce config: {', '.join(missing)}")

    def build_authorization_url(self, redirect_uri: str, state: str, code_challenge: str | None = None) -> str:
        self._validate_config()
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": SALESFORCE_OAUTH_SCOPES,
            "state": state,
        }
        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        return f"{self.instance_url}/services/oauth2/authorize?{urlencode(params)}"

    def exchange_authorization_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> dict[str, Any]:
        self._validate_config()
        data = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        }
        if code_verifier:
            data["code_verifier"] = code_verifier
        response = requests.post(
            f"{self.instance_url}/services/oauth2/token",
            data=data,
            timeout=20,
        )
        if not response.ok:
            raise SalesforceApiError("Salesforce authorization could not be completed.")
        data = response.json()
        self._store_token_response(data)
        return data

    def _store_token_response(self, data: dict[str, Any]) -> None:
        access_token = data.get("access_token")
        instance_url = data.get("instance_url") or self.instance_url
        if not access_token:
            raise SalesforceApiError("Salesforce OAuth response did not include an access token")

        if self.db is not None and self.workspace_id is not None:
            connection = self.connection
            if connection is None:
                connection = SalesforceConnection(workspace_id=self.workspace_id)
                self.db.add(connection)
                self.connection = connection
            connection.instance_url = instance_url.rstrip("/")
            connection.encrypted_access_token = encrypt_credentials({"token": access_token})
            try:
                expires_in = int(data.get("expires_in") or 0)
            except (TypeError, ValueError):
                expires_in = 0
            connection.access_token_expires_at = (
                datetime.now(timezone.utc) + timedelta(seconds=expires_in)
                if expires_in > 0 else None
            )
            if data.get("refresh_token"):
                connection.encrypted_refresh_token = encrypt_credentials({"token": data["refresh_token"]})
            now = datetime.now(timezone.utc)
            connection.status = "connected"
            connection.connected_at = connection.connected_at or now
            connection.last_connection_success_at = now
            connection.last_error = None
            self.db.flush()
        else:  # Compatibility for isolated service unit tests only.
            _TOKEN_CACHE["access_token"] = access_token
            _TOKEN_CACHE["instance_url"] = instance_url.rstrip("/")
            if data.get("refresh_token"):
                _TOKEN_CACHE["refresh_token"] = data["refresh_token"]

    def _token(self, kind: str) -> str | None:
        if self.connection is not None:
            if kind == "access" and self.connection.access_token_expires_at:
                expires_at = self.connection.access_token_expires_at
                now = datetime.now(timezone.utc) if expires_at.tzinfo else datetime.utcnow()
                if expires_at <= now:
                    return None
            encrypted = getattr(self.connection, f"encrypted_{kind}_token")
            return decrypt_credentials(encrypted).get("token")
        return _TOKEN_CACHE.get(f"{kind}_token")

    def _set_error(self, status: str, message: str) -> None:
        if self.connection is not None:
            self.connection.status = status
            self.connection.last_error = message[:500]
            if self.db is not None:
                self.db.commit()

    def _refresh_access_token(self) -> bool:
        self._validate_config()
        refresh_token = self._token("refresh")
        if not refresh_token:
            return False

        response = requests.post(
            f"{self.instance_url}/services/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": refresh_token,
            },
            timeout=20,
        )
        if not response.ok:
            if response.status_code in {400, 401}:
                self._set_error("reauthorization_required", "Salesforce authorization has expired or was revoked.")
            else:
                self._set_error("connection_error", "Salesforce could not refresh the connection.")
            return False

        self._store_token_response(response.json())
        if self.db is not None:
            self.db.commit()
        return True

    def _headers(self) -> dict[str, str]:
        access_token = self._token("access")
        if not access_token and not self._refresh_access_token():
            raise SalesforceConfigError(
                "Salesforce is not connected. Visit /api/salesforce/oauth/start to authorize BidLens."
            )
        return {
            "Authorization": f"Bearer {self._token('access')}",
            "Content-Type": "application/json",
        }

    def _api_url(self, path: str) -> str:
        instance_url = self.connected_instance_url
        if not instance_url and not self._refresh_access_token():
            raise SalesforceConfigError(
                "Salesforce is not connected. Visit /api/salesforce/oauth/start to authorize BidLens."
            )
        return f"{self.connected_instance_url}/services/data/{SALESFORCE_API_VERSION}/{path.lstrip('/')}"

    def opportunity_record_url(self, opportunity_id: str) -> str:
        instance_url = self.connected_instance_url
        if not instance_url and not self._refresh_access_token():
            raise SalesforceConfigError(
                "Salesforce is not connected. Visit /api/salesforce/oauth/start to authorize BidLens."
            )
        return f"{self.connected_instance_url}/lightning/r/Opportunity/{opportunity_id}/view"

    def is_authorized(self) -> bool:
        return bool(self._token("access") or self._refresh_access_token())

    @property
    def has_stored_authorization(self) -> bool:
        """Report local OAuth state without making a network request."""
        return bool(self._token("access") or self._token("refresh"))

    @property
    def connected_instance_url(self) -> str | None:
        if self.connection is not None:
            return self.connection.instance_url
        return _TOKEN_CACHE.get("instance_url") or self.instance_url or None

    def test_connection(self) -> dict[str, Any]:
        response = requests.get(
            self._api_url("limits"), headers=self._headers(), timeout=20,
        )
        if response.status_code == 401 and self._refresh_access_token():
            response = requests.get(self._api_url("limits"), headers=self._headers(), timeout=20)
        if not response.ok:
            if response.status_code == 401:
                self._set_error("reauthorization_required", "Salesforce authorization has expired or was revoked.")
            else:
                self._set_error("connection_error", "Salesforce could not validate the connection.")
            raise SalesforceApiError("Salesforce could not validate the connection.")
        if self.connection is not None:
            self.connection.status = "connected"
            self.connection.last_connection_success_at = datetime.now(timezone.utc)
            self.connection.last_error = None
            self.db.flush()
        return {"ok": True}

    def capture_identity_metadata(self, token_response: dict[str, Any]) -> None:
        """Best-effort capture of safe identity fields; authorization remains valid if unavailable."""
        if self.connection is None:
            return
        identity: dict[str, Any] = {}
        identity_url = token_response.get("id")
        access_token = self._token("access")
        if identity_url and access_token:
            try:
                response = requests.get(
                    identity_url,
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=20,
                )
                if response.ok:
                    identity = response.json()
            except requests.RequestException:
                pass
        self.connection.salesforce_org_id = identity.get("organization_id") or token_response.get("organization_id")
        self.connection.connected_user_id = identity.get("user_id") or token_response.get("user_id")
        self.connection.connected_username = (
            identity.get("display_name") or identity.get("username") or identity.get("email")
        )
        if self.db is not None:
            self.db.flush()

    def describe_opportunity(self) -> dict[str, Any]:
        response = requests.get(
            self._api_url("sobjects/Opportunity/describe"),
            headers=self._headers(),
            timeout=20,
        )
        if response.status_code == 401 and self._refresh_access_token():
            response = requests.get(
                self._api_url("sobjects/Opportunity/describe"),
                headers=self._headers(),
                timeout=20,
            )
        if not response.ok:
            raise SalesforceApiError(f"Salesforce Opportunity describe failed with status {response.status_code}.")
        return response.json()

    def required_createable_opportunity_fields(self) -> list[dict[str, Any]]:
        return self._required_createable_fields(
            self.describe_opportunity().get("fields") or []
        )

    @staticmethod
    def _required_createable_fields(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
        required_fields = []
        for field in fields:
            if (
                field.get("createable") is True
                and field.get("nillable") is False
                and field.get("defaultedOnCreate") is False
            ):
                required_fields.append(
                    {
                        "name": field.get("name"),
                        "label": field.get("label"),
                        "type": field.get("type"),
                    }
                )
        return required_fields

    def stage_name_values(self) -> list[str]:
        return self.opportunity_picklist_values("StageName")

    def opportunity_picklist_values(self, field_name: str) -> list[str]:
        return self._picklist_values(
            self.describe_opportunity().get("fields") or [],
            field_name,
        )

    @staticmethod
    def _picklist_values(
        fields: list[dict[str, Any]],
        field_name: str,
    ) -> list[str]:
        for field in fields:
            if field.get("name") == field_name:
                return [
                    value.get("value")
                    for value in field.get("picklistValues") or []
                    if value.get("active") is True and value.get("value")
                ]
        return []

    def inspect_opportunity_requirements(self) -> dict[str, Any]:
        """Reuse Opportunity metadata validation with a single describe request."""
        fields = self.describe_opportunity().get("fields") or []
        field_by_name = {
            field.get("name"): field
            for field in fields
            if field.get("name")
        }
        required_fields = self._required_createable_fields(fields)
        valid_stage_names = self._picklist_values(fields, "StageName")
        intake_source_values = self._picklist_values(fields, "Intake_Source_c__c")
        selected_intake_source = (
            "BidLens"
            if "BidLens" in intake_source_values
            else intake_source_values[0] if intake_source_values else None
        )
        provided_fields = {
            "Name",
            "StageName",
            "CloseDate",
            "External_Source_ID_c__c",
            "Intake_Status__c",
            "Intake_Source_c__c",
            "Description",
        }
        missing_required_fields = [
            field
            for field in required_fields
            if field.get("name") and field["name"] not in provided_fields
        ]
        mapped_field_names = (
            "StageName",
            "CloseDate",
            "External_Source_ID_c__c",
            "Intake_Status__c",
            "Intake_Source_c__c",
        )
        unavailable_mapped_fields = [
            field_name
            for field_name in mapped_field_names
            if field_name not in field_by_name
            or field_by_name[field_name].get("createable") is not True
        ]
        return {
            "auth_available": True,
            "required_fields": required_fields,
            "valid_stage_names": valid_stage_names,
            "intake_source_values": intake_source_values,
            "selected_intake_source": selected_intake_source,
            "missing_required_fields": missing_required_fields,
            "unavailable_mapped_fields": unavailable_mapped_fields,
            "default_stage_valid": "Prospecting" in valid_stage_names,
            "required_fields_verified": not missing_required_fields,
            "field_mappings_valid": not unavailable_mapped_fields,
            "error": None,
        }

    @staticmethod
    def _escape_soql_literal(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")

    @staticmethod
    def _opportunity_from_record(record: dict[str, Any]) -> SalesforceOpportunity:
        return SalesforceOpportunity(
            id=record["Id"],
            name=record.get("Name"),
            external_source_id=record.get("External_Source_ID_c__c"),
            intake_status=record.get("Intake_Status__c"),
        )

    def find_opportunity_by_external_source_id(self, source_record_id: str) -> SalesforceOpportunity | None:
        escaped_source_id = self._escape_soql_literal(source_record_id)
        soql = (
            "SELECT Id, Name, External_Source_ID_c__c, Intake_Status__c "
            "FROM Opportunity "
            f"WHERE External_Source_ID_c__c = '{escaped_source_id}' "
            "LIMIT 1"
        )
        response = requests.get(
            self._api_url("query"),
            headers=self._headers(),
            params={"q": soql},
            timeout=20,
        )
        if response.status_code == 401 and self._refresh_access_token():
            response = requests.get(
                self._api_url("query"),
                headers=self._headers(),
                params={"q": soql},
                timeout=20,
            )
        if not response.ok:
            raise SalesforceApiError(f"Salesforce Opportunity query failed with status {response.status_code}.")

        records = response.json().get("records") or []
        if not records:
            return None
        return self._opportunity_from_record(records[0])

    def update_intake_status(self, opportunity_id: str, intake_status: str = PROSPECT_FEED_STATUS) -> None:
        self.update_opportunity(
            opportunity_id,
            {"Intake_Status__c": intake_status},
        )

    def update_opportunity(self, opportunity_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = requests.patch(
            self._api_url(f"sobjects/Opportunity/{opportunity_id}"),
            headers=self._headers(),
            json=payload,
            timeout=20,
        )
        if response.status_code == 401 and self._refresh_access_token():
            response = requests.patch(
                self._api_url(f"sobjects/Opportunity/{opportunity_id}"),
                headers=self._headers(),
                json=payload,
                timeout=20,
            )
        if response.status_code != 204:
            raise SalesforceApiError(
                f"Salesforce Opportunity update failed with status {response.status_code}."
            )
        return {"status_code": response.status_code, "accepted": True}

    def create_opportunity(self, payload: dict[str, Any]) -> str:
        response = requests.post(
            self._api_url("sobjects/Opportunity"),
            headers=self._headers(),
            json=payload,
            timeout=20,
        )
        if response.status_code == 401 and self._refresh_access_token():
            response = requests.post(
                self._api_url("sobjects/Opportunity"),
                headers=self._headers(),
                json=payload,
                timeout=20,
            )
        if response.status_code != 201:
            raise SalesforceApiError(
                f"Salesforce Opportunity create failed with status {response.status_code}."
            )

        record_id = response.json().get("id")
        if not record_id:
            raise SalesforceApiError("Salesforce Opportunity create response did not include an id")
        return record_id
