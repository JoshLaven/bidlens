from __future__ import annotations

from datetime import datetime
from typing import Any


FIELD_LABELS = {
    "response_deadline": "Due date",
    "description_text": "Synopsis",
    "description": "Description",
    "source_stage": "Status",
    "opportunity_type": "Opportunity type",
    "set_aside": "Set-aside",
    "source_url": "Source URL",
    "sam_url": "SAM URL",
    "description_url": "Solicitation documents",
    "solicitation_number": "Solicitation number",
}


def _field_label(field_name: str) -> str:
    return FIELD_LABELS.get(field_name, field_name.replace("_", " ").title())


def _meaningful_update_reason(audit: dict[str, Any]) -> str:
    changed_fields = audit.get("changed_fields") or {}
    if isinstance(changed_fields, dict):
        field_names = list(changed_fields)
    else:
        field_names = list(changed_fields or [])
    if not field_names:
        return "Existing opportunity updated"
    labels = [_field_label(field_name) for field_name in field_names]
    if len(labels) == 1:
        return f"Existing opportunity updated · {labels[0]} changed"
    if len(labels) == 2:
        return f"Existing opportunity updated · {labels[0]} and {labels[1]} changed"
    return f"Existing opportunity updated · {len(labels)} meaningful changes recorded"


def build_upsert_detail(
    *,
    source: str,
    data: dict[str, Any],
    status: str,
    audit: dict[str, Any] | None = None,
    reason_code: str | None = None,
) -> dict[str, Any]:
    audit = audit or {}
    normalized_status = {
        "inserted": "created",
        "created": "created",
        "updated": "updated",
        "unchanged": "unchanged",
        "skipped": "skipped_duplicate",
    }.get(status, status)

    if reason_code and reason_code.startswith("cross_source_sam_notice_match"):
        reason = "Existing opportunity matched by authoritative SAM Notice ID; GovWin duplicate not created"
    elif normalized_status == "created":
        reason = "New opportunity created"
    elif normalized_status == "unchanged":
        reason = "Existing opportunity matched by source + source_record_id; no meaningful changes"
    elif normalized_status == "updated":
        if audit.get("salesforce_linked"):
            sync_status = audit.get("salesforce_sync_status")
            suffix = f"; Salesforce sync {sync_status}" if sync_status else ""
            reason = f"Existing linked Salesforce opportunity updated{suffix}"
        elif audit.get("update_event_id") or audit.get("changed_fields"):
            reason = _meaningful_update_reason(audit)
        else:
            reason = "Existing opportunity refreshed with non-user-facing source changes"
    elif reason_code == "duplicate_within_import":
        reason = "Duplicate row within same import file"
    else:
        reason = "Duplicate source record was not created"

    detail = {
        "source": source,
        "source_record_id": data.get("source_record_id"),
        "title": data.get("title"),
        "result": normalized_status,
        "reason": reason,
        "matched_opportunity_id": audit.get("matched_opportunity_id"),
        "changed_fields_json": audit.get("changed_fields") or None,
        "error_message": audit.get("salesforce_error"),
        "processed_at": datetime.utcnow(),
    }
    if audit.get("update_event_id"):
        detail["_update_event_id"] = audit["update_event_id"]
    return detail


def build_invalid_detail(
    *,
    source: str,
    source_record_id: str | None,
    title: str | None,
    reason: str,
) -> dict[str, Any]:
    return {
        "source": source,
        "source_record_id": source_record_id,
        "title": title,
        "result": "skipped_invalid",
        "reason": reason,
        "processed_at": datetime.utcnow(),
    }


def build_error_detail(
    *,
    source: str,
    source_record_id: str | None,
    title: str | None,
    error: Exception | str,
) -> dict[str, Any]:
    message = str(error)
    return {
        "source": source,
        "source_record_id": source_record_id,
        "title": title,
        "result": "error",
        "reason": f"Import failed: {message}",
        "error_message": message,
        "processed_at": datetime.utcnow(),
    }
