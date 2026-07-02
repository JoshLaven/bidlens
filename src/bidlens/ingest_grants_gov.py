from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import requests
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .grants_gov_client import GrantsGovApiError, fetch_opportunity_detail, search_recent_opportunities
from .models import Opportunity
from .services.ingestion_details import build_error_detail, build_invalid_detail, build_upsert_detail
from .services.opportunity_monitor import apply_source_update
from .services.qualification import new_opportunity_qualification_status
from .services.pursuit_lanes import refresh_opportunity_lane_matches


SOURCE = "grants_gov"
logger = logging.getLogger(__name__)


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_value(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload.get(key) not in (None, ""):
            return payload.get(key)
    lowered = {str(k).lower(): v for k, v in payload.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def _get_path(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = payload
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
        if value in (None, ""):
            return None
    return value


def _first_path(payload: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        value = _get_path(payload, path)
        if value not in (None, ""):
            return value
    return None


def _parse_date(value: Any) -> dt.date | None:
    text = _clean(value)
    if not text:
        return None
    candidates = [text]
    if "," in text and " " in text:
        candidates.append(text.rsplit(" ", 1)[0])
    for candidate in candidates:
        candidate_values = (candidate, candidate[:19], candidate[:10])
        for fmt in (
            "%m/%d/%Y",
            "%Y-%m-%d",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d-%H-%M-%S",
            "%b %d, %Y %I:%M:%S %p",
        ):
            for candidate_value in candidate_values:
                try:
                    return dt.datetime.strptime(candidate_value, fmt).date()
                except ValueError:
                    pass
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _extract_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []

    for key in (
        "oppHits",
        "opportunities",
        "opportunityList",
        "data",
        "results",
        "items",
        "records",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _extract_records(value)
            if nested:
                return nested
    return []


def _source_url(record: dict[str, Any], source_record_id: str) -> str | None:
    url = _clean(
        _first_value(
            record,
            "sourceUrl",
            "source_url",
            "url",
            "link",
            "opportunityUrl",
            "opportunityURL",
        )
    )
    if url:
        return url
    return f"https://www.grants.gov/search-results-detail/{source_record_id}"


def _merge_detail_payload(record: dict[str, Any], detail_payload: dict[str, Any] | None) -> dict[str, Any]:
    if not detail_payload:
        return record
    safe_detail_payload = {**detail_payload}
    safe_detail_payload.pop("token", None)
    detail_data = detail_payload.get("data") if isinstance(detail_payload, dict) else None
    if not isinstance(detail_data, dict):
        return {**record, "search_result": record, "detail_payload": safe_detail_payload}

    merged = {**record, **detail_data}
    merged["search_result"] = record
    merged["detail_payload"] = safe_detail_payload
    return merged


def normalize_grants_gov_record(record: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    source_record_id = _clean(
        _first_value(record, "id", "opportunityId", "opportunityID", "opportunity_id", "oppId", "opp_id")
    )
    title = _clean(_first_value(record, "title", "opportunityTitle", "opportunity_title", "oppTitle"))
    agency = _clean(
        _first_value(record, "agency", "agencyName", "agency_name", "agencyCode", "agency_code")
        or _first_path(record, ("synopsis", "agencyName"), ("agencyDetails", "agencyName"))
    )
    opportunity_number = _clean(
        _first_value(record, "opportunityNumber", "opportunity_number", "oppNumber", "opp_number", "number")
        or _first_path(record, ("synopsis", "opportunityNumber"))
    )
    posted_date = _parse_date(
        _first_value(record, "postedDate", "posted_date", "openDate", "open_date", "postingDate")
        or _first_path(record, ("synopsis", "postingDate"), ("synopsis", "postedDate"))
    )
    response_deadline = _parse_date(
        _first_value(
            record,
            "closeDate",
            "close_date",
            "responseDate",
            "responseDeadline",
            "response_deadline",
            "dueDate",
            "due_date",
        )
        or _first_path(record, ("synopsis", "responseDate"), ("synopsis", "closeDate"))
    )
    description = _clean(
        _first_value(
            record,
            "description",
            "descriptionText",
            "synopsisDesc",
            "synopsisDescription",
            "summary",
            "additionalInformation",
            "additionalInfo",
            "additionalInformationOnEligibility",
        )
        or _first_path(
            record,
            ("synopsis", "synopsisDesc"),
            ("synopsis", "description"),
            ("synopsis", "descriptionText"),
            ("synopsis", "additionalInformation"),
            ("synopsis", "additionalInfo"),
            ("synopsis", "additionalInformationOnEligibility"),
            ("synopsis", "applicantEligibilityDesc"),
        )
    )

    if not source_record_id:
        return None, "missing Grants.gov opportunity identifier"
    if not title:
        return None, "missing title"
    if not agency:
        return None, "missing agency"

    date_fallback = posted_date or response_deadline or dt.date.today()
    return {
        "source": SOURCE,
        "source_record_id": source_record_id,
        "solicitation_number": opportunity_number,
        "source_url": _source_url(record, source_record_id),
        "raw_source_payload": record,
        "sam_notice_id": None,
        "govwin_staging_id": None,
        "title": title,
        "agency": agency,
        "opportunity_type": "Grant",
        "posted_date": posted_date or date_fallback,
        "response_deadline": response_deadline or date_fallback,
        "naics": None,
        "naics_title": None,
        "set_aside": None,
        "description": description,
        "description_url": None,
        "description_text": description,
        "sam_url": None,
    }, None


def upsert_grants_gov_opportunity(
    db: Session,
    organization_id: int,
    data: dict[str, Any],
    *,
    audit: dict[str, Any] | None = None,
) -> str:
    existing = (
        db.query(Opportunity)
        .filter(
            Opportunity.organization_id == organization_id,
            Opportunity.source == SOURCE,
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
                refresh_opportunity_lane_matches(db, organization_id, opportunity)
                if audit is not None:
                    audit.update({
                        "matched_opportunity_id": opportunity.id,
                        "salesforce_linked": False,
                        "changed_fields": {},
                    })
            return "created"
        except IntegrityError:
            if audit is not None:
                audit["integrity_error"] = True
            logger.info("Skipping duplicate Grants.gov source_record_id=%s", data["source_record_id"])
            return "skipped"

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
        return "updated"
    return "unchanged"


def enrich_grants_gov_opportunity_detail(db: Session, opportunity: Opportunity) -> bool:
    if opportunity.source != SOURCE or not opportunity.source_record_id:
        return False

    detail_payload = fetch_opportunity_detail(opportunity.source_record_id)
    current_payload = opportunity.raw_source_payload if isinstance(opportunity.raw_source_payload, dict) else {}
    merged = _merge_detail_payload(current_payload, detail_payload)
    normalized, reason = normalize_grants_gov_record(merged)
    if reason or not normalized:
        logger.info(
            "Grants.gov detail enrichment skipped source_record_id=%s reason=%s",
            opportunity.source_record_id,
            reason,
        )
        return False

    monitored_fields = (
        "description",
        "description_text",
        "solicitation_number",
        "source_url",
        "posted_date",
        "response_deadline",
    )
    monitor_result = apply_source_update(
        db,
        opportunity,
        normalized,
        monitored_fields=monitored_fields,
    )
    db.commit()
    return monitor_result.changed


def ingest_grants_gov(db: Session, *, organization_id: int, days_back: int = 14, rows: int = 25) -> dict[str, Any]:
    payload = search_recent_opportunities(days_back=days_back, rows=rows)
    records = _extract_records(payload)
    result = {
        "status": "success",
        "organization_id": organization_id,
        "received": len(records),
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        "errors": 0,
        "detail_errors": 0,
        "skipped_reasons": [],
        "_record_details": [],
    }
    for index, record in enumerate(records, start=1):
        detail_lookup_error = None
        source_record_id = _clean(
            _first_value(record, "id", "opportunityId", "opportunityID", "opportunity_id", "oppId", "opp_id")
        )
        if source_record_id:
            try:
                detail_payload = fetch_opportunity_detail(source_record_id)
                record = _merge_detail_payload(record, detail_payload)
            except (GrantsGovApiError, requests.RequestException) as exc:
                detail_lookup_error = str(exc)
                result["detail_errors"] += 1
                result["skipped_reasons"].append(
                    {"row": index, "reason": f"detail lookup failed for {source_record_id}: {exc}"}
                )
                logger.warning(
                    "Grants.gov detail lookup failed source_record_id=%s error=%s",
                    source_record_id,
                    exc,
                )
        normalized, reason = normalize_grants_gov_record(record)
        if reason:
            result["skipped"] += 1
            result["skipped_reasons"].append({"row": index, "reason": reason})
            result["_record_details"].append(build_invalid_detail(
                source="grants.gov",
                source_record_id=source_record_id,
                title=_clean(_first_value(record, "title", "opportunityTitle", "opportunity_title")),
                reason=reason,
            ))
            continue
        try:
            audit: dict[str, Any] = {}
            status = upsert_grants_gov_opportunity(db, organization_id, normalized, audit=audit)
            if status == "created":
                result["created"] += 1
            elif status == "updated":
                result["updated"] += 1
            elif status == "unchanged":
                result["unchanged"] += 1
            else:
                result["skipped"] += 1
            record_detail = build_upsert_detail(
                source="grants.gov",
                data=normalized,
                status=status,
                audit=audit,
            )
            if detail_lookup_error and not record_detail.get("error_message"):
                record_detail["error_message"] = f"Detail lookup failed: {detail_lookup_error}"
            result["_record_details"].append(record_detail)
        except Exception as exc:
            result["errors"] += 1
            result["skipped_reasons"].append({"row": index, "reason": repr(exc)})
            result["_record_details"].append(build_error_detail(
                source="grants.gov",
                source_record_id=normalized.get("source_record_id"),
                title=normalized.get("title"),
                error=exc,
            ))
            logger.exception("Grants.gov record failed source_record_id=%s", normalized.get("source_record_id"))
    db.commit()
    result["message"] = (
        f"Grants.gov pull completed: {result['received']} received, {result['created']} created, "
        f"{result['updated']} updated, {result['unchanged']} unchanged, "
        f"{result['skipped']} skipped, {result['errors']} errors."
    )
    return result
