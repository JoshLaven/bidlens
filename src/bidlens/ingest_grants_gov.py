from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import requests
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .grants_gov_client import GrantsGovApiError, fetch_opportunity_detail, search_recent_opportunities
from .models import Opportunity, OpportunityHistoryEvent
from .services.ingestion_details import build_error_detail, build_invalid_detail, build_upsert_detail
from .services.opportunity_history import (
    EVENT_GRANTS_FORECAST_VERSION,
    EVENT_GRANTS_SYNOPSIS_VERSION,
    record_history_event,
    record_imported_history,
)
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


def _parse_grants_history_datetime(value: Any) -> dt.datetime | None:
    text = _clean(value)
    if not text:
        return None
    for timezone_suffix in (" EDT", " EST", " UTC", " GMT"):
        if text.endswith(timezone_suffix):
            text = text[: -len(timezone_suffix)]
            break
    for fmt in (
        "%b %d, %Y %I:%M:%S %p",
        "%Y-%m-%d-%H-%M-%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError:
        return None


def _grants_version_history_entries(raw_payload: dict[str, Any]) -> list[dict[str, Any]]:
    history = raw_payload.get("opportunityHistoryDetails")
    historical_versions = history if isinstance(history, list) else []
    candidates = [*historical_versions, raw_payload]
    entries: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for history_type, event_type, label in (
            ("synopsis", EVENT_GRANTS_SYNOPSIS_VERSION, "Synopsis"),
            ("forecast", EVENT_GRANTS_FORECAST_VERSION, "Forecast"),
        ):
            version_payload = candidate.get(history_type)
            if not isinstance(version_payload, dict):
                continue
            version = version_payload.get("version")
            if version in (None, ""):
                continue
            updated_date = (
                version_payload.get("lastUpdatedDate")
                or version_payload.get("actionDate")
                or version_payload.get("createTimeStamp")
                or version_payload.get("postingDate")
            )
            version_key = f"{history_type}:{version}:{updated_date or ''}"
            if version_key in seen_keys:
                continue
            seen_keys.add(version_key)
            modification_description = _clean(
                version_payload.get("modComments")
                or version_payload.get("modificationComments")
                or candidate.get("modComments")
            )
            modified_fields = candidate.get(f"{history_type}ModifiedFields")
            if not isinstance(modified_fields, list):
                modified_fields = []
            entries.append({
                "event_type": event_type,
                "occurred_at": _parse_grants_history_datetime(updated_date),
                "event_data": {
                    "source_version_key": version_key,
                    "history_type": history_type,
                    "version": version,
                    "version_name": f"{label} {version}",
                    "updated_date": str(updated_date) if updated_date else None,
                    "modification_description": modification_description,
                    "modified_fields": modified_fields,
                    "source_revision": candidate.get("revision"),
                },
            })

    return entries


def sync_grants_gov_version_history(
    db: Session,
    opportunity: Opportunity,
    raw_payload: dict[str, Any] | None,
    *,
    notify_interested: bool,
) -> int:
    if opportunity.source != SOURCE or not isinstance(raw_payload, dict):
        return 0

    existing_keys = {
        event_data.get("source_version_key")
        for (event_data,) in (
            db.query(OpportunityHistoryEvent.event_data)
            .filter(
                OpportunityHistoryEvent.organization_id == opportunity.organization_id,
                OpportunityHistoryEvent.opportunity_id == opportunity.id,
                OpportunityHistoryEvent.event_type.in_(
                    (EVENT_GRANTS_SYNOPSIS_VERSION, EVENT_GRANTS_FORECAST_VERSION)
                ),
            )
            .all()
        )
        if isinstance(event_data, dict)
    }
    entries = _grants_version_history_entries(raw_payload)
    latest_key = None
    if entries:
        latest_entry = max(
            entries,
            key=lambda entry: entry["occurred_at"] or dt.datetime.min,
        )
        latest_key = latest_entry["event_data"]["source_version_key"]

    created = 0
    for entry in entries:
        event_data = entry["event_data"]
        if event_data["source_version_key"] in existing_keys:
            continue
        record_history_event(
            db,
            opportunity=opportunity,
            event_type=entry["event_type"],
            source=SOURCE,
            event_data=event_data,
            occurred_at=entry["occurred_at"],
            notify_interested=(
                notify_interested
                and event_data["source_version_key"] == latest_key
            ),
        )
        existing_keys.add(event_data["source_version_key"])
        created += 1
    return created


def backfill_stored_grants_gov_version_history(
    db: Session,
    *,
    organization_id: int,
) -> int:
    """Map version data already stored in raw payloads without another API request."""
    opportunities = (
        db.query(Opportunity)
        .filter(
            Opportunity.organization_id == organization_id,
            Opportunity.source == SOURCE,
        )
        .all()
    )
    return sum(
        sync_grants_gov_version_history(
            db,
            opportunity,
            opportunity.raw_source_payload,
            notify_interested=False,
        )
        for opportunity in opportunities
    )


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
                record_imported_history(db, opportunity)
                sync_grants_gov_version_history(
                    db,
                    opportunity,
                    data.get("raw_source_payload"),
                    notify_interested=False,
                )
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
    version_events_created = sync_grants_gov_version_history(
        db,
        existing,
        data.get("raw_source_payload"),
        notify_interested=True,
    )
    if version_events_created and not monitor_result.changed:
        existing.raw_source_payload = data.get("raw_source_payload")
        db.flush()
    if audit is not None:
        audit.update({
            "matched_opportunity_id": existing.id,
            "salesforce_linked": bool(existing.salesforce_opportunity_id),
            "changed_fields": monitor_result.changed_fields,
            "salesforce_sync_status": monitor_result.salesforce_sync_status,
            "salesforce_error": monitor_result.salesforce_error,
            "update_event_id": monitor_result.update_event_id,
        })
    if monitor_result.changed or version_events_created:
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


def _search_hit_count(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    candidates = (
        data.get("hitCount") if isinstance(data, dict) else None,
        payload.get("hitCount"),
        payload.get("totalRecords"),
    )
    for value in candidates:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return None


def _fetch_daily_search_results(*, days_back: int, rows: int) -> tuple[list[dict[str, Any]], int]:
    page_size = max(1, int(rows))
    start_record_num = 0
    records: list[dict[str, Any]] = []
    pages_pulled = 0

    while True:
        payload = search_recent_opportunities(
            days_back=days_back,
            rows=page_size,
            start_record_num=start_record_num,
        )
        pages_pulled += 1
        page_records = _extract_records(payload)
        records.extend(page_records)

        next_start = start_record_num + len(page_records)
        hit_count = _search_hit_count(payload)
        if not page_records:
            break
        if hit_count is not None and next_start >= hit_count:
            break
        if hit_count is None and len(page_records) < page_size:
            break
        if next_start <= start_record_num:
            break
        start_record_num = next_start

    return records, pages_pulled


def ingest_grants_gov(db: Session, *, organization_id: int, days_back: int = 1, rows: int = 25) -> dict[str, Any]:
    records, pages_pulled = _fetch_daily_search_results(days_back=days_back, rows=rows)
    result = {
        "status": "success",
        "organization_id": organization_id,
        "received": len(records),
        "pages_pulled": pages_pulled,
        "date_range_days": days_back,
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
    result["history_events_backfilled"] = backfill_stored_grants_gov_version_history(
        db,
        organization_id=organization_id,
    )
    db.commit()
    result["message"] = (
        f"Grants.gov pull completed: {result['received']} received, {result['created']} created, "
        f"{result['updated']} updated, {result['unchanged']} unchanged, "
        f"{result['skipped']} skipped, {result['errors']} errors."
    )
    return result
