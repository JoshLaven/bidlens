from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ..models import IngestionRun, IngestionRunDetail, OpportunityUpdateEvent


def record_source_activity(
    db: Session,
    *,
    source: str,
    organization_id: int,
    user_id: int | None,
    filename: str | None = None,
    result: dict[str, Any] | None = None,
    run_id: int | None = None,
    processed_count: int | None = None,
    created_count: int | None = None,
    updated_count: int | None = None,
    unchanged_count: int | None = None,
    skipped_count: int | None = None,
    error_count: int | None = None,
    reason_counts: dict[str, int] | None = None,
    reason_labels: dict[str, str] | None = None,
    notes: str | None = None,
) -> IngestionRun:
    result = result or {}
    detail_payloads = list(result.pop("_record_details", []) or [])
    now = datetime.utcnow()
    run = db.query(IngestionRun).filter(IngestionRun.id == run_id).first() if run_id else None
    if run is None:
        run = IngestionRun(source=source, started_at=now)
        db.add(run)

    run.source = source
    run.organization_id = organization_id
    run.user_id = user_id
    run.filename = filename or run.filename
    run.finished_at = now
    run.processed_count = int(processed_count if processed_count is not None else result.get("processed", 0) or 0)
    run.created_count = int(created_count if created_count is not None else result.get("created", 0) or 0)
    run.updated_count = int(updated_count if updated_count is not None else result.get("updated", 0) or 0)
    run.unchanged_count = int(unchanged_count if unchanged_count is not None else result.get("unchanged", 0) or 0)
    run.skipped_count = int(skipped_count if skipped_count is not None else result.get("skipped", 0) or 0)
    run.error_count = int(error_count if error_count is not None else result.get("errors", 0) or 0)
    run.inserted_count = run.created_count + run.updated_count
    run.filtered_count = int(result.get("filtered", 0) or 0)
    if reason_counts or reason_labels:
        run.reason_summary_json = {
            "reason_counts": reason_counts or {},
            "reason_labels": reason_labels or {},
        }
    elif result.get("reason_counts") or result.get("reason_labels"):
        run.reason_summary_json = {
            "reason_counts": dict(result.get("reason_counts") or {}),
            "reason_labels": dict(result.get("reason_labels") or {}),
        }
    run.notes = notes if notes is not None else result.get("message") or run.notes
    db.flush()
    update_event_ids = [
        detail.get("_update_event_id")
        for detail in detail_payloads
        if detail.get("_update_event_id")
    ]
    if update_event_ids:
        (
            db.query(OpportunityUpdateEvent)
            .filter(
                OpportunityUpdateEvent.organization_id == organization_id,
                OpportunityUpdateEvent.id.in_(update_event_ids),
            )
            .update(
                {OpportunityUpdateEvent.ingestion_run_id: run.id},
                synchronize_session=False,
            )
        )
    if detail_payloads:
        db.add_all(
            [
                IngestionRunDetail(
                    ingestion_run_id=run.id,
                    source=str(detail.get("source") or source),
                    source_record_id=detail.get("source_record_id"),
                    title=detail.get("title"),
                    result=str(detail.get("result") or "error"),
                    reason=str(detail.get("reason") or "No reason recorded"),
                    matched_opportunity_id=detail.get("matched_opportunity_id"),
                    changed_fields_json=detail.get("changed_fields_json"),
                    error_message=detail.get("error_message"),
                    processed_at=detail.get("processed_at") or now,
                )
                for detail in detail_payloads
            ]
        )
    result["run_id"] = run.id
    return run
