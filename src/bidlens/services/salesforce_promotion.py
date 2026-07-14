from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ..models import Opportunity
from ..sam_client import _is_url_like
from . import push_opportunity_to_crm
from .opportunity_history import (
    EVENT_SALESFORCE_SYNCHRONIZED,
    record_history_event,
)
from .salesforce import (
    PROSPECT_FEED_STATUS,
    SalesforceApiError,
    SalesforceConfigError,
    SalesforceService,
)


class SalesforceCreateValidationError(ValueError):
    def __init__(self, detail: dict[str, Any]):
        super().__init__(str(detail.get("message") or "Salesforce create validation failed"))
        self.detail = detail


@dataclass(frozen=True)
class SalesforcePromotionResult:
    outcome: str
    message: str
    salesforce_opportunity_id: str | None = None
    salesforce_opportunity_url: str | None = None
    salesforce_action: str | None = None
    salesforce_status: str | None = None
    salesforce_opportunity_name: str | None = None
    previous_intake_status: str | None = None
    new_intake_status: str | None = None
    selected_intake_source: str | None = None
    intake_source_values: list[str] | None = None
    created_payload_summary: dict[str, Any] | None = None

    def as_response_payload(self) -> dict[str, Any]:
        return {
            "salesforce_outcome": self.outcome,
            "salesforce_message": self.message,
            "salesforce_opportunity_id": self.salesforce_opportunity_id,
            "salesforce_opportunity_url": self.salesforce_opportunity_url,
            "salesforce_action": self.salesforce_action,
            "salesforce_status": self.salesforce_status,
            "salesforce_opportunity_name": self.salesforce_opportunity_name,
            "previous_intake_status": self.previous_intake_status,
            "new_intake_status": self.new_intake_status,
            "selected_intake_source": self.selected_intake_source,
            "intake_source_values": self.intake_source_values,
            "created_payload_summary": self.created_payload_summary,
        }


def _best_description_text(opp: Opportunity) -> str:
    description_text = (opp.description_text or "").strip()
    if description_text:
        return description_text

    description = (opp.description or "").strip()
    if description and not _is_url_like(description):
        return description

    return ""


def _salesforce_opportunity_name(opp: Opportunity) -> str:
    name = (opp.title or f"BidLens Opportunity {opp.id}").strip()
    if len(name) <= 120:
        return name
    return f"{name[:117]}..."


def salesforce_payload_summary(payload: dict[str, Any]) -> dict[str, Any]:
    summary = dict(payload)
    if summary.get("Description"):
        description = str(summary["Description"])
        summary["Description"] = description[:500] + ("..." if len(description) > 500 else "")
        summary["description_included"] = True
    return summary


def _select_intake_source_value(values: list[str]) -> str | None:
    if "BidLens" in values:
        return "BidLens"
    return values[0] if values else None


def build_salesforce_create_payload(
    service: SalesforceService,
    opp: Opportunity,
) -> tuple[dict[str, Any], str, list[str]]:
    close_date = opp.response_deadline or (date.today() + timedelta(days=30))
    payload: dict[str, Any] = {
        "Name": _salesforce_opportunity_name(opp),
        "StageName": "Prospecting",
        "CloseDate": close_date.isoformat(),
        "External_Source_ID_c__c": (opp.source_record_id or "").strip(),
        "Intake_Status__c": PROSPECT_FEED_STATUS,
    }
    description = _best_description_text(opp)
    if description:
        payload["Description"] = description[:32000]

    required_fields = service.required_createable_opportunity_fields()
    valid_stage_names = service.stage_name_values()
    intake_source_values = service.opportunity_picklist_values("Intake_Source_c__c")
    selected_intake_source = _select_intake_source_value(intake_source_values)
    if "Prospecting" not in valid_stage_names:
        raise SalesforceCreateValidationError({
            "message": "Salesforce StageName 'Prospecting' is not valid in this org.",
            "valid_stage_names": valid_stage_names,
            "created": False,
        })
    if not selected_intake_source:
        raise SalesforceCreateValidationError({
            "message": "Salesforce Intake_Source_c__c has no active picklist values.",
            "intake_source_values": intake_source_values,
            "created": False,
        })
    payload["Intake_Source_c__c"] = selected_intake_source

    provided_fields = set(payload)
    missing_required_fields = [
        field
        for field in required_fields
        if field.get("name") and field["name"] not in provided_fields
    ]
    if missing_required_fields:
        raise SalesforceCreateValidationError({
            "message": "Salesforce Opportunity has required createable fields outside this POC payload.",
            "missing_required_fields": missing_required_fields,
            "required_fields": required_fields,
            "created": False,
        })
    return payload, selected_intake_source, intake_source_values


def _record_salesforce_opportunity_reference(
    db: Session,
    *,
    opp: Opportunity,
    salesforce_opp_id: str,
    salesforce_opp_url: str,
    action: str,
) -> None:
    opp.salesforce_opportunity_id = salesforce_opp_id
    opp.salesforce_opportunity_url = salesforce_opp_url
    opp.salesforce_synced_at = datetime.utcnow()
    opp.salesforce_action = action
    db.commit()


def _record_salesforce_synchronized_history(
    db: Session,
    *,
    opp: Opportunity,
    salesforce_opp_id: str | None,
    action: str,
    error_type: str | None = None,
) -> None:
    event_data = {
        "salesforce_opportunity_id": salesforce_opp_id,
        "action": action,
    }
    if error_type:
        event_data["error_type"] = error_type
    record_history_event(
        db,
        opportunity=opp,
        event_type=EVENT_SALESFORCE_SYNCHRONIZED,
        source="salesforce",
        event_data=event_data,
    )
    db.commit()


def _validate_opportunity_for_salesforce(
    *,
    opp: Opportunity,
    organization_id: int,
) -> str:
    if opp.organization_id != organization_id:
        raise ValueError("Opportunity does not belong to this workspace")
    source_record_id = (opp.source_record_id or "").strip()
    if not source_record_id:
        raise ValueError("Opportunity is missing source_record_id")
    if opp.qualification_status != "qualified":
        raise ValueError("Opportunity must be qualified before CRM actions")
    if opp.decision_state == "ARCHIVED":
        raise ValueError("Cannot push archived opportunities to CRM")
    return source_record_id


def ensure_opportunity_in_salesforce(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    opportunity: Opportunity,
    ui_version: str = "v1",
    service: SalesforceService | None = None,
) -> SalesforcePromotionResult:
    source_record_id = _validate_opportunity_for_salesforce(
        opp=opportunity,
        organization_id=organization_id,
    )

    service = service or SalesforceService()
    if not service.is_authorized():
        raise SalesforceConfigError("Salesforce is not connected.")

    if opportunity.salesforce_opportunity_id:
        salesforce_opp_id = opportunity.salesforce_opportunity_id
        service.update_intake_status(salesforce_opp_id, PROSPECT_FEED_STATUS)
        salesforce_url = (
            opportunity.salesforce_opportunity_url
            or service.opportunity_record_url(salesforce_opp_id)
        )
        _record_salesforce_opportunity_reference(
            db,
            opp=opportunity,
            salesforce_opp_id=salesforce_opp_id,
            salesforce_opp_url=salesforce_url,
            action="pushed",
        )
        push_opportunity_to_crm(
            db,
            org_id=organization_id,
            user_id=user_id,
            opp_id=opportunity.id,
            ui_version=ui_version,
        )
        _record_salesforce_synchronized_history(
            db,
            opp=opportunity,
            salesforce_opp_id=salesforce_opp_id,
            action="updated",
        )
        return SalesforcePromotionResult(
            outcome="already_linked",
            message="Updated existing Salesforce opportunity",
            salesforce_opportunity_id=salesforce_opp_id,
            salesforce_opportunity_url=salesforce_url,
            salesforce_action="pushed",
            salesforce_status="Pushed to Salesforce",
            new_intake_status=PROSPECT_FEED_STATUS,
        )

    sf_opp = service.find_opportunity_by_external_source_id(source_record_id)
    if sf_opp:
        service.update_intake_status(sf_opp.id, PROSPECT_FEED_STATUS)
        salesforce_url = service.opportunity_record_url(sf_opp.id)
        _record_salesforce_opportunity_reference(
            db,
            opp=opportunity,
            salesforce_opp_id=sf_opp.id,
            salesforce_opp_url=salesforce_url,
            action="pushed",
        )
        push_opportunity_to_crm(
            db,
            org_id=organization_id,
            user_id=user_id,
            opp_id=opportunity.id,
            ui_version=ui_version,
        )
        _record_salesforce_synchronized_history(
            db,
            opp=opportunity,
            salesforce_opp_id=sf_opp.id,
            action="matched_existing",
        )
        return SalesforcePromotionResult(
            outcome="matched_existing",
            message="Linked existing Salesforce opportunity",
            salesforce_opportunity_id=sf_opp.id,
            salesforce_opportunity_url=salesforce_url,
            salesforce_action="pushed",
            salesforce_status="Pushed to Salesforce",
            salesforce_opportunity_name=sf_opp.name,
            previous_intake_status=sf_opp.intake_status,
            new_intake_status=PROSPECT_FEED_STATUS,
        )

    payload, selected_intake_source, intake_source_values = build_salesforce_create_payload(
        service,
        opportunity,
    )
    salesforce_opp_id = service.create_opportunity(payload)
    salesforce_url = service.opportunity_record_url(salesforce_opp_id)
    _record_salesforce_opportunity_reference(
        db,
        opp=opportunity,
        salesforce_opp_id=salesforce_opp_id,
        salesforce_opp_url=salesforce_url,
        action="created",
    )
    push_opportunity_to_crm(
        db,
        org_id=organization_id,
        user_id=user_id,
        opp_id=opportunity.id,
        ui_version=ui_version,
    )
    _record_salesforce_synchronized_history(
        db,
        opp=opportunity,
        salesforce_opp_id=salesforce_opp_id,
        action="created",
    )
    return SalesforcePromotionResult(
        outcome="created",
        message="Created Salesforce opportunity",
        salesforce_opportunity_id=salesforce_opp_id,
        salesforce_opportunity_url=salesforce_url,
        salesforce_action="created",
        salesforce_status="Created in Salesforce",
        selected_intake_source=selected_intake_source,
        intake_source_values=intake_source_values,
        created_payload_summary=salesforce_payload_summary(payload),
    )


def record_salesforce_sync_failure(
    db: Session,
    *,
    opportunity: Opportunity,
    error: Exception,
) -> None:
    _record_salesforce_synchronized_history(
        db,
        opp=opportunity,
        salesforce_opp_id=opportunity.salesforce_opportunity_id,
        action="failed",
        error_type=error.__class__.__name__,
    )


def is_salesforce_configuration_error(error: Exception) -> bool:
    return isinstance(error, SalesforceConfigError)


def is_salesforce_api_error(error: Exception) -> bool:
    return isinstance(error, SalesforceApiError)
