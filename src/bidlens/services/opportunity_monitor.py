from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from ..models import Opportunity, OpportunityUpdateEvent
from .salesforce import SalesforceService


logger = logging.getLogger(__name__)

# These are the normalized source fields that can change the current BidLens
# record. raw_source_payload is retained only when one of these fields changes,
# so transport-only payload differences do not create false updates.
DEFAULT_MONITORED_FIELDS = (
    "solicitation_number",
    "source_url",
    "sam_notice_id",
    "govwin_staging_id",
    "title",
    "agency",
    "opportunity_type",
    "posted_date",
    "response_deadline",
    "naics",
    "naics_title",
    "set_aside",
    "account_type",
    "account_type_confidence",
    "account_type_source",
    "description",
    "description_url",
    "description_text",
    "sam_url",
)


@dataclass(frozen=True)
class OpportunityMonitorResult:
    changed: bool
    changed_fields: dict[str, dict[str, Any]]
    salesforce_sync_status: str | None = None
    salesforce_error: str | None = None
    update_event_id: int | None = None


def _json_value(value: Any) -> Any:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def _salesforce_name(opportunity: Opportunity) -> str:
    name = (opportunity.title or f"BidLens Opportunity {opportunity.id}").strip()
    return name if len(name) <= 120 else f"{name[:117]}..."


def _salesforce_payload(
    opportunity: Opportunity,
    changed_field_names: set[str],
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if "title" in changed_field_names:
        payload["Name"] = _salesforce_name(opportunity)
    if "response_deadline" in changed_field_names and opportunity.response_deadline:
        payload["CloseDate"] = opportunity.response_deadline.isoformat()
    if changed_field_names.intersection({"description", "description_text"}):
        description = (opportunity.description_text or opportunity.description or "").strip()
        if description:
            payload["Description"] = description[:32000]
    return payload


def _audit_response(value: Any) -> Any:
    if value is None:
        return {"accepted": True}
    if isinstance(value, (dict, list, str, int, float, bool)):
        return value
    return {"accepted": True}


def apply_source_update(
    db: Session,
    opportunity: Opportunity,
    incoming: dict[str, Any],
    *,
    monitored_fields: Iterable[str] = DEFAULT_MONITORED_FIELDS,
    excluded_fields: Iterable[str] = (),
    observed_at: dt.datetime | None = None,
) -> OpportunityMonitorResult:
    """Apply one normalized source observation to an existing opportunity."""
    now = observed_at or dt.datetime.utcnow()
    opportunity.last_seen_at = now
    excluded = set(excluded_fields)
    changes: dict[str, dict[str, Any]] = {}

    for field_name in monitored_fields:
        if field_name in excluded or field_name not in incoming:
            continue
        incoming_value = incoming.get(field_name)
        # Preserve the importers' established behavior: absent source values do
        # not erase a previously populated BidLens value.
        if incoming_value is None:
            continue
        current_value = getattr(opportunity, field_name)
        if current_value != incoming_value:
            changes[field_name] = {
                "before": _json_value(current_value),
                "after": _json_value(incoming_value),
            }

    if not changes:
        # updated_at has a mapper-level onupdate hook. Explicitly retain its
        # current value so an observation-only write changes last_seen_at alone.
        opportunity.updated_at = opportunity.updated_at
        flag_modified(opportunity, "updated_at")
        return OpportunityMonitorResult(changed=False, changed_fields={})

    for field_name in changes:
        setattr(opportunity, field_name, incoming[field_name])
    if "raw_source_payload" in incoming:
        opportunity.raw_source_payload = incoming["raw_source_payload"]
    opportunity.upserted_at = now

    db.flush()
    salesforce_payload = (
        _salesforce_payload(opportunity, set(changes))
        if opportunity.salesforce_opportunity_id
        else None
    )
    event = OpportunityUpdateEvent(
        organization_id=opportunity.organization_id,
        opportunity_id=opportunity.id,
        source=opportunity.source,
        source_record_id=opportunity.source_record_id,
        detected_at=now,
        changed_fields=changes,
        salesforce_payload=salesforce_payload,
        salesforce_sync_status=(
            "pending" if opportunity.salesforce_opportunity_id else "not_linked"
        ),
    )
    db.add(event)
    db.flush()

    if not opportunity.salesforce_opportunity_id:
        return OpportunityMonitorResult(
            changed=True,
            changed_fields=changes,
            salesforce_sync_status=event.salesforce_sync_status,
            update_event_id=event.id,
        )

    try:
        if salesforce_payload:
            response = SalesforceService().update_opportunity(
                opportunity.salesforce_opportunity_id,
                salesforce_payload,
            )
            event.salesforce_response = _audit_response(response)
        else:
            event.salesforce_response = {
                "accepted": True,
                "message": "No Salesforce-owned field changed; no API request was required.",
            }
        event.salesforce_sync_status = "succeeded"
        event.salesforce_synced_at = now
        opportunity.salesforce_synced_at = now
        logger.info(
            "Opportunity monitor Salesforce sync succeeded opportunity_id=%s source=%s "
            "source_record_id=%s salesforce_opportunity_id=%s changed_fields=%s",
            opportunity.id,
            opportunity.source,
            opportunity.source_record_id,
            opportunity.salesforce_opportunity_id,
            sorted(changes),
        )
    except Exception as exc:
        event.salesforce_sync_status = "failed"
        event.salesforce_error = str(exc)
        event.salesforce_response = {"error": str(exc)}
        logger.exception(
            "Opportunity monitor Salesforce sync failed opportunity_id=%s source=%s "
            "source_record_id=%s salesforce_opportunity_id=%s changed_fields=%s error=%s",
            opportunity.id,
            opportunity.source,
            opportunity.source_record_id,
            opportunity.salesforce_opportunity_id,
            sorted(changes),
            exc,
        )

    return OpportunityMonitorResult(
        changed=True,
        changed_fields=changes,
        salesforce_sync_status=event.salesforce_sync_status,
        salesforce_error=event.salesforce_error,
        update_event_id=event.id,
    )
