from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from .account_type_classifier import classify_account_type
from .opportunity_stages import govwin_display_stage


SOURCE = "govwin_api"


class GovWinAdapter:
    """Boundary for all future GovWin Web Services communication."""

    def __init__(self, credentials: dict[str, str]):
        self.credentials = credentials

    def test_connection(self) -> dict[str, Any]:
        missing = [
            key
            for key in ("client_id", "client_secret", "username", "password")
            if not self.credentials.get(key)
        ]
        if missing:
            return {
                "connected": False,
                "status": "not_connected",
                "message": "GovWin credentials are incomplete.",
            }
        return {
            "connected": True,
            "status": "connected",
            "message": "Mock GovWin connection succeeded.",
        }

    def list_saved_searches(self) -> list[dict[str, str]]:
        return [
            {
                "id": "mock-health-research",
                "name": "Health and Human Services Research",
            }
        ]

    def sync_saved_search(self, saved_search_id: str | None = None) -> list[dict[str, Any]]:
        search_id = saved_search_id or self.list_saved_searches()[0]["id"]
        today = date.today()
        return [
            {
                "opportunity_id": "GW-MOCK-1001",
                "title": "Mock Public Health Research Services",
                "agency": "Department of Health and Human Services",
                "opportunity_type": "Post-RFP",
                "posted_date": today.isoformat(),
                "response_deadline": (today + timedelta(days=45)).isoformat(),
                "solicitation_number": "GW-MOCK-PH-1001",
                "description": "Mock GovWin Web Services opportunity used to validate integration flow.",
                "source_url": "https://iq.govwin.com/neo/opportunity/view/GW-MOCK-1001",
                "saved_search_id": search_id,
            },
            {
                "opportunity_id": "GW-MOCK-1002",
                "title": "Mock State Education Evaluation Support",
                "agency": "State of Arizona Department of Education",
                "opportunity_type": "Pre-RFP",
                "posted_date": today.isoformat(),
                "response_deadline": (today + timedelta(days=60)).isoformat(),
                "solicitation_number": "GW-MOCK-ED-1002",
                "description": "Mock GovWin opportunity for state education research and evaluation.",
                "source_url": "https://iq.govwin.com/neo/opportunity/view/GW-MOCK-1002",
                "saved_search_id": search_id,
            },
        ]

    def normalize_opportunity(self, opportunity: dict[str, Any]) -> dict[str, Any]:
        opportunity_id = str(opportunity["opportunity_id"]).strip()
        agency = str(opportunity.get("agency") or "Unknown Agency").strip()
        account_type = classify_account_type(agency)
        posted_date = self._date_value(opportunity.get("posted_date")) or date.today()
        response_deadline = (
            self._date_value(opportunity.get("response_deadline"))
            or posted_date + timedelta(days=30)
        )
        description = str(opportunity.get("description") or "").strip() or None
        source_stage = self._optional_text(
            opportunity.get("source_stage") or opportunity.get("opportunity_type")
        )
        return {
            "source": SOURCE,
            "source_record_id": opportunity_id,
            "govwin_staging_id": opportunity_id,
            "solicitation_number": self._optional_text(opportunity.get("solicitation_number")),
            "source_url": self._optional_text(opportunity.get("source_url")),
            "raw_source_payload": dict(opportunity),
            "title": str(opportunity.get("title") or f"GovWin Opportunity {opportunity_id}").strip(),
            "agency": agency,
            "opportunity_type": govwin_display_stage(source_stage) or "RFP",
            "source_stage": source_stage,
            "posted_date": posted_date,
            "response_deadline": response_deadline,
            "naics": self._optional_text(opportunity.get("naics")),
            "naics_title": self._optional_text(opportunity.get("naics_title")),
            "set_aside": self._optional_text(opportunity.get("set_aside")),
            "account_type": account_type.account_type,
            "account_type_confidence": account_type.confidence,
            "account_type_source": account_type.source,
            "description": description,
            "description_url": None,
            "description_text": description,
            "sam_notice_id": self._optional_text(opportunity.get("sam_notice_id")),
            "sam_url": self._optional_text(opportunity.get("sam_url")),
        }

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    @staticmethod
    def _date_value(value: Any) -> date | None:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
        except ValueError:
            return None
