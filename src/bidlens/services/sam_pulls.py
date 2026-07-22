from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy.orm import Session

from ..ingest_sam import ingest_sam
from ..models import SamSourceConfig
from .ingestion_runs import record_source_activity
from .job_runs import sanitize_error_message
from .sam_source_config import ingest_kwargs


def sam_busy_payload(*, organization_id: int) -> dict[str, Any]:
    return {
        "status": "busy",
        "organization_id": organization_id,
        "message": "A SAM pull is already in progress. Wait for it to finish before starting another.",
        "run_id": None,
        "inserted": 0,
        "updated": 0,
        "skipped": 0,
        "filtered": 0,
        "errors": 0,
        "results": [],
    }


def find_sam_source_config(
    db: Session,
    *,
    organization_id: int,
    search_id: int | None = None,
) -> SamSourceConfig | None:
    query = db.query(SamSourceConfig).filter(SamSourceConfig.organization_id == organization_id)
    if search_id is not None:
        query = query.filter(SamSourceConfig.id == search_id)
    return query.order_by(SamSourceConfig.name.asc(), SamSourceConfig.id.asc()).first()


def execute_sam_source_pull(
    db: Session,
    *,
    organization_id: int,
    config: SamSourceConfig,
    run_type: str,
    manual_pull: bool,
    enrich_descriptions: bool = False,
) -> dict[str, Any]:
    return ingest_sam(
        db,
        organization_id=organization_id,
        manual_pull=manual_pull,
        enrich_descriptions=enrich_descriptions,
        saved_search_name=config.name,
        run_type=run_type,
        source_config_id=config.id,
        **ingest_kwargs(config),
    )


def retry_after_display(retry_after: str | None, retry_after_seconds: float | None) -> str | None:
    if retry_after:
        return retry_after
    if retry_after_seconds is None:
        return None

    retry_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=retry_after_seconds)
    return retry_at.strftime("%a, %d %b %Y %H:%M:%S GMT")


def retry_after_header_value(retry_after: str | None, retry_after_seconds: float | None) -> str | None:
    if retry_after:
        return retry_after
    if retry_after_seconds is None:
        return None
    return str(int(round(retry_after_seconds)))


def failed_naics(results: list[dict[str, Any]]) -> list[str]:
    return [item.get("naics") for item in results if item.get("naics")]


def record_sam_source_activity(
    db: Session,
    *,
    organization_id: int,
    user_id: int | None,
    result: dict[str, Any],
    run_type: str | None = None,
) -> None:
    search_name = result.get("saved_search_name")
    label = run_type or result.get("run_type") or "Manual"
    record_source_activity(
        db,
        source="sam.gov",
        organization_id=organization_id,
        user_id=user_id,
        filename=(
            f"{label} saved search: {search_name}"
            if search_name
            else f"{label} SAM.gov pull"
        ),
        result=result,
        run_id=result.get("run_id"),
        processed_count=int(result.get("records_seen", 0) or 0),
        created_count=int(result.get("inserted", 0) or 0),
        updated_count=int(result.get("updated", 0) or 0),
        unchanged_count=int(result.get("unchanged", 0) or 0),
        skipped_count=int(result.get("skipped", 0) or 0),
        error_count=int(result.get("errors", 0) or 0),
        notes=result.get("message"),
    )
    db.commit()


def record_sam_noop_activity(
    db: Session,
    *,
    organization_id: int,
    user_id: int | None,
    reason: str,
    message: str,
    run_type: str = "Manual",
) -> dict[str, Any]:
    result = {
        "status": "noop",
        "organization_id": organization_id,
        "message": message,
        "run_id": None,
        "inserted": 0,
        "updated": 0,
        "skipped": 0,
        "filtered": 0,
        "errors": 1,
        "records_seen": 0,
        "results": [],
        "run_type": run_type,
    }
    record_source_activity(
        db,
        source="sam.gov",
        organization_id=organization_id,
        user_id=user_id,
        filename=f"{run_type} SAM.gov pull",
        result=result,
        processed_count=0,
        created_count=0,
        updated_count=0,
        unchanged_count=0,
        skipped_count=0,
        error_count=1,
        reason_counts={reason: 1},
        reason_labels={reason: message},
        notes=message,
    )
    db.commit()
    return result


def record_sam_failure_activity(
    db: Session,
    *,
    organization_id: int,
    config: SamSourceConfig | None,
    error: Exception,
    run_type: str = "Scheduled",
) -> None:
    safe_error = sanitize_error_message(str(error)) or type(error).__name__
    result = {
        "status": "failed",
        "organization_id": organization_id,
        "message": f"SAM.gov pull failed: {safe_error}",
        "run_id": None,
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        "filtered": 0,
        "errors": 1,
        "records_seen": 0,
        "results": [],
        "saved_search_name": config.name if config else None,
        "run_type": run_type,
    }
    record_sam_source_activity(
        db,
        organization_id=organization_id,
        user_id=None,
        result=result,
        run_type=run_type,
    )
