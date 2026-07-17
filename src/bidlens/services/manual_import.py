from __future__ import annotations

import csv
import datetime as dt
import io
from collections import Counter
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Opportunity
from .account_type_classifier import classify_account_type
from .ingestion_details import build_error_detail, build_invalid_detail, build_upsert_detail
from .opportunity_history import record_imported_history
from .opportunity_monitor import apply_source_update
from .opportunity_stages import normalize_display_stage
from .pursuit_lanes import refresh_opportunity_lane_matches
from .qualification import new_opportunity_qualification_status


SOURCE = "manual_import"
TEMPLATE_HEADERS = (
    "source",
    "source_record_id",
    "title",
    "agency",
    "opportunity_type",
    "posted_date",
    "response_deadline",
    "description",
    "source_url",
    "solicitation_number",
    "naics",
    "naics_title",
    "set_aside",
)
REASON_LABELS = {
    "new_opportunity": "New opportunity",
    "existing_manual_record_changed": "Existing manual record changed",
    "existing_manual_record": "Existing manual record",
    "missing_source_record_id": "Missing Source Record ID",
    "missing_title": "Missing Title",
    "missing_agency": "Missing Agency",
    "missing_posted_date": "Missing Posted Date",
    "missing_response_deadline": "Missing Response Deadline",
    "invalid_posted_date": "Invalid Posted Date",
    "invalid_response_deadline": "Invalid Response Deadline",
    "duplicate_within_import": "Duplicate row within same import file",
    "integrity_error": "Duplicate or integrity error",
    "import_error": "Import error",
}


def csv_template_text() -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(TEMPLATE_HEADERS)
    writer.writerow([
        SOURCE,
        "manual-001",
        "Example opportunity title",
        "Example Agency",
        "RFP",
        "2026-07-01",
        "2026-08-15",
        "Short opportunity description",
        "https://example.com/opportunity/manual-001",
        "SOL-001",
        "541611",
        "Administrative Management and General Management Consulting Services",
        "",
    ])
    return output.getvalue()


def parse_csv_rows(file_bytes: bytes) -> list[dict[str, Any]]:
    text = file_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    return [
        {str(key or "").strip(): value for key, value in row.items() if key}
        for row in reader
        if any(str(value or "").strip() for value in row.values())
    ]


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_date(value: Any) -> tuple[dt.date | None, bool]:
    text = _clean(value)
    if not text:
        return None, False
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return dt.datetime.strptime(text, fmt).date(), True
        except ValueError:
            pass
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date(), True
    except ValueError:
        return None, True


def _raw_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items()}


def _normalize_row(row: dict[str, Any], row_number: int) -> tuple[dict[str, Any] | None, str | None]:
    source = _clean(row.get("source")) or SOURCE
    source_record_id = _clean(row.get("source_record_id"))
    title = _clean(row.get("title"))
    agency = _clean(row.get("agency"))
    opportunity_type = _clean(row.get("opportunity_type")) or "RFP"

    if not source_record_id:
        return None, "missing_source_record_id"
    if not title:
        return None, "missing_title"
    if not agency:
        return None, "missing_agency"

    posted_date, posted_provided = _parse_date(row.get("posted_date"))
    response_deadline, response_provided = _parse_date(row.get("response_deadline"))
    if not posted_provided:
        return None, "missing_posted_date"
    if posted_date is None:
        return None, "invalid_posted_date"
    if not response_provided:
        return None, "missing_response_deadline"
    if response_deadline is None:
        return None, "invalid_response_deadline"

    account_type = classify_account_type(agency)
    payload = _raw_payload(row)
    payload["_bidlens_import"] = {
        "source": source,
        "row_number": row_number,
        "account_type_reason": account_type.reason,
    }

    return {
        "source": source,
        "source_record_id": source_record_id,
        "solicitation_number": _clean(row.get("solicitation_number")),
        "source_url": _clean(row.get("source_url")),
        "raw_source_payload": payload,
        "title": title,
        "agency": agency,
        "opportunity_type": normalize_display_stage(
            source=source,
            opportunity_type=opportunity_type,
            source_stage=_clean(row.get("source_stage")),
        ),
        "source_stage": _clean(row.get("source_stage")) or opportunity_type,
        "posted_date": posted_date,
        "response_deadline": response_deadline,
        "naics": _clean(row.get("naics")),
        "naics_title": _clean(row.get("naics_title")),
        "set_aside": _clean(row.get("set_aside")),
        "account_type": account_type.account_type,
        "account_type_confidence": account_type.confidence,
        "account_type_source": account_type.source,
        "description": _clean(row.get("description")),
        "description_url": _clean(row.get("description_url")),
        "description_text": _clean(row.get("description")),
        "sam_notice_id": _clean(row.get("sam_notice_id")),
        "sam_url": _clean(row.get("sam_url")),
    }, None


def upsert_manual_opportunity(
    db: Session,
    organization_id: int,
    data: dict[str, Any],
    *,
    audit: dict[str, Any] | None = None,
) -> tuple[str, Opportunity | None, str]:
    existing = (
        db.query(Opportunity)
        .filter(
            Opportunity.organization_id == organization_id,
            Opportunity.source == data["source"],
            Opportunity.source_record_id == data["source_record_id"],
        )
        .one_or_none()
    )

    if existing is None:
        try:
            with db.begin_nested():
                opportunity = Opportunity(
                    organization_id=organization_id,
                    **data,
                    qualification_status=new_opportunity_qualification_status(db, organization_id),
                    upserted_at=dt.datetime.utcnow(),
                    last_seen_at=dt.datetime.utcnow(),
                )
                db.add(opportunity)
                db.flush()
                record_imported_history(db, opportunity)
                refresh_opportunity_lane_matches(db, organization_id, opportunity)
                if audit is not None:
                    audit.update({"matched_opportunity_id": opportunity.id, "changed_fields": {}})
            return "created", opportunity, "new_opportunity"
        except IntegrityError:
            return "skipped", None, "integrity_error"

    monitor_result = apply_source_update(db, existing, data)
    if audit is not None:
        audit.update({
            "matched_opportunity_id": existing.id,
            "salesforce_linked": bool(existing.salesforce_opportunity_id),
            "changed_fields": monitor_result.changed_fields,
            "salesforce_sync_status": monitor_result.salesforce_sync_status,
            "salesforce_error": monitor_result.salesforce_error,
            "update_event_id": monitor_result.update_event_id,
        })
    if monitor_result.changed:
        refresh_opportunity_lane_matches(db, organization_id, existing)
        return "updated", existing, "existing_manual_record_changed"
    return "unchanged", existing, "existing_manual_record"


def import_manual_csv(db: Session, organization_id: int, file_bytes: bytes) -> dict[str, Any]:
    rows = parse_csv_rows(file_bytes)
    reason_counts: Counter[str] = Counter()
    result = {
        "processed": len(rows),
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        "errors": 0,
        "skipped_reasons": [],
        "duplicate_diagnostics": [],
        "reason_counts": {},
        "reason_labels": REASON_LABELS,
        "_record_details": [],
    }
    seen_source_records: set[tuple[str, str]] = set()
    for index, row in enumerate(rows, start=2):
        normalized, reason = _normalize_row(row, index)
        if reason:
            reason_counts[reason] += 1
            result["skipped"] += 1
            result["skipped_reasons"].append({
                "row": index,
                "reason": REASON_LABELS.get(reason, reason),
                "reason_code": reason,
            })
            result["_record_details"].append(build_invalid_detail(
                source=_clean(row.get("source")) or SOURCE,
                source_record_id=_clean(row.get("source_record_id")),
                title=_clean(row.get("title")),
                reason=REASON_LABELS.get(reason, reason),
            ))
            continue

        source_key = (normalized["source"], normalized["source_record_id"])
        if source_key in seen_source_records:
            reason_code = "duplicate_within_import"
            reason_counts[reason_code] += 1
            result["skipped"] += 1
            result["_record_details"].append(build_upsert_detail(
                source=normalized["source"],
                data=normalized,
                status="skipped",
                reason_code=reason_code,
            ))
            continue
        seen_source_records.add(source_key)

        audit: dict[str, Any] = {}
        try:
            status, _opportunity, reason_code = upsert_manual_opportunity(
                db,
                organization_id,
                normalized,
                audit=audit,
            )
        except Exception as exc:
            reason_counts["import_error"] += 1
            result["errors"] += 1
            result["_record_details"].append(build_error_detail(
                source=normalized["source"],
                source_record_id=normalized["source_record_id"],
                title=normalized.get("title"),
                error=exc,
            ))
            continue

        reason_counts[reason_code] += 1
        if status == "created":
            result["created"] += 1
        elif status == "updated":
            result["updated"] += 1
        elif status == "unchanged":
            result["unchanged"] += 1
        else:
            result["skipped"] += 1
            result["skipped_reasons"].append({
                "row": index,
                "reason": REASON_LABELS.get(reason_code, "Duplicate or integrity error"),
                "reason_code": reason_code,
            })
        result["_record_details"].append(build_upsert_detail(
            source=normalized["source"],
            data=normalized,
            status=status,
            audit=audit,
            reason_code=reason_code,
        ))

    result["reason_counts"] = dict(reason_counts)
    return result
