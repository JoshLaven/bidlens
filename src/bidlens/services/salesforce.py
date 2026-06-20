from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import secrets
from urllib.parse import urlencode
from typing import Any

import requests

from .. import config


SALESFORCE_API_VERSION = "v60.0"
PROSPECT_FEED_STATUS = "Prospect_Feed"
SALESFORCE_OAUTH_SCOPES = "api refresh_token"
_TOKEN_CACHE: dict[str, str] = {}


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
    ) -> None:
        self.instance_url = (instance_url or config.SALESFORCE_INSTANCE_URL or "").rstrip("/")
        self.client_id = client_id or config.SALESFORCE_CLIENT_ID
        self.client_secret = client_secret or config.SALESFORCE_CLIENT_SECRET

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

    def exchange_authorization_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> None:
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
            raise SalesforceApiError(f"Salesforce OAuth token exchange failed: {response.status_code} {response.text}")
        self._store_token_response(response.json())

    def _store_token_response(self, data: dict[str, Any]) -> None:
        access_token = data.get("access_token")
        instance_url = data.get("instance_url") or self.instance_url
        if not access_token:
            raise SalesforceApiError("Salesforce OAuth response did not include an access token")

        _TOKEN_CACHE["access_token"] = access_token
        _TOKEN_CACHE["instance_url"] = instance_url.rstrip("/")
        if data.get("refresh_token"):
            _TOKEN_CACHE["refresh_token"] = data["refresh_token"]

    def _refresh_access_token(self) -> bool:
        self._validate_config()
        refresh_token = _TOKEN_CACHE.get("refresh_token")
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
            return False

        self._store_token_response(response.json())
        return True

    def _headers(self) -> dict[str, str]:
        access_token = _TOKEN_CACHE.get("access_token")
        if not access_token and not self._refresh_access_token():
            raise SalesforceConfigError(
                "Salesforce is not connected. Visit /api/salesforce/oauth/start to authorize BidLens."
            )
        return {
            "Authorization": f"Bearer {_TOKEN_CACHE['access_token']}",
            "Content-Type": "application/json",
        }

    def _api_url(self, path: str) -> str:
        instance_url = _TOKEN_CACHE.get("instance_url")
        if not instance_url and not self._refresh_access_token():
            raise SalesforceConfigError(
                "Salesforce is not connected. Visit /api/salesforce/oauth/start to authorize BidLens."
            )
        return f"{_TOKEN_CACHE['instance_url']}/services/data/{SALESFORCE_API_VERSION}/{path.lstrip('/')}"

    def opportunity_record_url(self, opportunity_id: str) -> str:
        instance_url = _TOKEN_CACHE.get("instance_url")
        if not instance_url and not self._refresh_access_token():
            raise SalesforceConfigError(
                "Salesforce is not connected. Visit /api/salesforce/oauth/start to authorize BidLens."
            )
        return f"{_TOKEN_CACHE['instance_url']}/lightning/r/Opportunity/{opportunity_id}/view"

    def is_authorized(self) -> bool:
        return bool(_TOKEN_CACHE.get("access_token") or self._refresh_access_token())

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
            raise SalesforceApiError(f"Salesforce Opportunity describe failed: {response.status_code} {response.text}")
        return response.json()

    def required_createable_opportunity_fields(self) -> list[dict[str, Any]]:
        required_fields = []
        for field in self.describe_opportunity().get("fields") or []:
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
        for field in self.describe_opportunity().get("fields") or []:
            if field.get("name") == field_name:
                return [
                    value.get("value")
                    for value in field.get("picklistValues") or []
                    if value.get("active") is True and value.get("value")
                ]
        return []

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
            raise SalesforceApiError(f"Salesforce Opportunity query failed: {response.status_code} {response.text}")

        records = response.json().get("records") or []
        if not records:
            return None
        return self._opportunity_from_record(records[0])

    def update_intake_status(self, opportunity_id: str, intake_status: str = PROSPECT_FEED_STATUS) -> None:
        response = requests.patch(
            self._api_url(f"sobjects/Opportunity/{opportunity_id}"),
            headers=self._headers(),
            json={"Intake_Status__c": intake_status},
            timeout=20,
        )
        if response.status_code == 401 and self._refresh_access_token():
            response = requests.patch(
                self._api_url(f"sobjects/Opportunity/{opportunity_id}"),
                headers=self._headers(),
                json={"Intake_Status__c": intake_status},
                timeout=20,
            )
        if response.status_code != 204:
            raise SalesforceApiError(
                f"Salesforce Opportunity update failed: {response.status_code} {response.text}"
            )

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
                f"Salesforce Opportunity create failed: {response.status_code} {response.text}"
            )

        record_id = response.json().get("id")
        if not record_id:
            raise SalesforceApiError("Salesforce Opportunity create response did not include an id")
        return record_id
