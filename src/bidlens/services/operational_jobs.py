from __future__ import annotations

from collections import Counter, defaultdict
import datetime as dt
from typing import Any, Callable

from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..grants_gov_client import DEFAULT_GRANTS_POSTED_DAYS_BACK, DEFAULT_GRANTS_ROWS
from ..ingest_grants_gov import ingest_grants_gov
from ..models import (
    DailySnapshot,
    DailyBriefEmailDelivery,
    GrantsSourceConfig,
    IngestionRun,
    Organization,
    OrganizationMembership,
    SamSourceConfig,
    User,
    Workspace,
)
from .daily_brief_emails import build_daily_brief_email_message, is_valid_recipient_email
from .daily_snapshot import create_daily_snapshot
from .email_delivery import EmailSender, ResendEmailSender
from .ingestion_runs import record_source_activity
from .job_runs import (
    JOB_STATUS_FAILED,
    JOB_STATUS_PARTIAL_SUCCESS,
    JOB_STATUS_PAUSED,
    JOB_STATUS_SKIPPED,
    JOB_STATUS_SUCCESS,
    JOB_TYPE_DAILY_SNAPSHOT,
    JOB_TYPE_DAILY_BRIEF_EMAIL,
    JOB_TYPE_GRANTS_INGEST,
    JOB_TYPE_SAM_INGEST,
    TRIGGER_TYPE_SCHEDULED,
    complete_job_run,
    fail_job_run,
    sanitize_error_message,
    start_job_run,
)
from .sam_pulls import execute_sam_source_pull, record_sam_failure_activity


def _print(message: str) -> None:
    print(message, flush=True)


def _job_exit_code(status_counts: Counter[str]) -> int:
    if status_counts.get(JOB_STATUS_FAILED) or status_counts.get(JOB_STATUS_PARTIAL_SUCCESS):
        return 1
    return 0


def _job_run_status(source_status: str | None) -> str:
    normalized = str(source_status or "").strip().lower()
    if normalized in {"success", "completed"}:
        return JOB_STATUS_SUCCESS
    if normalized in {"partial_success", "warning"}:
        return JOB_STATUS_PARTIAL_SUCCESS
    if normalized in {"paused_rate_limit", "paused"}:
        return JOB_STATUS_PAUSED
    if normalized in {"no_records", "skipped"}:
        return JOB_STATUS_SKIPPED
    if normalized in {"failed", "error"}:
        return JOB_STATUS_FAILED
    return JOB_STATUS_PARTIAL_SUCCESS


def _job_summary(status_counts: Counter[str]) -> str:
    return (
        f"Organizations processed: {sum(status_counts.values())}; "
        f"Success: {status_counts.get(JOB_STATUS_SUCCESS, 0)}; "
        f"Paused: {status_counts.get(JOB_STATUS_PAUSED, 0)}; "
        f"Skipped: {status_counts.get(JOB_STATUS_SKIPPED, 0)}; "
        f"Partial success: {status_counts.get(JOB_STATUS_PARTIAL_SUCCESS, 0)}; "
        f"Failed: {status_counts.get(JOB_STATUS_FAILED, 0)}"
    )


def _sam_details(result: dict[str, Any], *, source_configs_processed: int) -> dict[str, Any]:
    paused = result.get("status") == "paused_rate_limit" or bool(result.get("stopped_due_to_rate_limit"))
    return {
        "source_configs_processed": source_configs_processed,
        "records_seen": int(result.get("records_seen", 0) or 0),
        "created": int(result.get("inserted", result.get("created", 0)) or 0),
        "updated": int(result.get("updated", 0) or 0),
        "unchanged": int(result.get("unchanged", 0) or 0),
        "skipped": int(result.get("skipped", 0) or 0),
        "filtered": int(result.get("filtered", 0) or 0),
        "errors": int(result.get("errors", 0) or 0),
        "pages_pulled": int(result.get("pages_pulled", 0) or 0),
        "search_requests_made": int(result.get("search_requests_made", 0) or 0),
        "checkpoint_saved": paused,
        "pause_reason": "rate_limit" if paused else None,
        "ingestion_run_ids": [result["run_id"]] if result.get("run_id") else [],
    }


def _combine_sam_details(details: list[dict[str, Any]]) -> dict[str, Any]:
    combined = {
        "source_configs_processed": len(details),
        "records_seen": 0,
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        "filtered": 0,
        "errors": 0,
        "pages_pulled": 0,
        "search_requests_made": 0,
        "checkpoint_saved": False,
        "pause_reason": None,
        "ingestion_run_ids": [],
    }
    for detail in details:
        for key in (
            "records_seen",
            "created",
            "updated",
            "unchanged",
            "skipped",
            "filtered",
            "errors",
            "pages_pulled",
            "search_requests_made",
        ):
            combined[key] += int(detail.get(key, 0) or 0)
        combined["checkpoint_saved"] = combined["checkpoint_saved"] or bool(detail.get("checkpoint_saved"))
        combined["pause_reason"] = combined["pause_reason"] or detail.get("pause_reason")
        combined["ingestion_run_ids"].extend(detail.get("ingestion_run_ids") or [])
    return combined


def _combine_statuses(statuses: list[str]) -> str:
    if not statuses:
        return JOB_STATUS_SKIPPED
    if any(status == JOB_STATUS_FAILED for status in statuses):
        if any(status in {JOB_STATUS_SUCCESS, JOB_STATUS_PAUSED, JOB_STATUS_SKIPPED} for status in statuses):
            return JOB_STATUS_PARTIAL_SUCCESS
        return JOB_STATUS_FAILED
    if any(status == JOB_STATUS_PARTIAL_SUCCESS for status in statuses):
        return JOB_STATUS_PARTIAL_SUCCESS
    if any(status == JOB_STATUS_PAUSED for status in statuses):
        return JOB_STATUS_PAUSED
    if all(status == JOB_STATUS_SKIPPED for status in statuses):
        return JOB_STATUS_SKIPPED
    return JOB_STATUS_SUCCESS


def _grants_reason_summary(result: dict[str, Any]) -> tuple[dict[str, int] | None, dict[str, str] | None]:
    reason_counts: dict[str, int] = {}
    reason_labels: dict[str, str] = {}
    if result.get("status") == "no_records":
        reason_counts["no_records"] = 1
        reason_labels["no_records"] = result.get("message", "No records returned")
    if int(result.get("detail_errors", 0) or 0):
        reason_counts["detail_lookup_error"] = int(result.get("detail_errors", 0) or 0)
        reason_labels["detail_lookup_error"] = "One or more Grants.gov detail lookups failed"
    return reason_counts or None, reason_labels or None


def _record_grants_ingestion_run(db: Session, *, organization_id: int, result: dict[str, Any]) -> IngestionRun:
    reason_counts, reason_labels = _grants_reason_summary(result)
    run = record_source_activity(
        db,
        source="grants.gov",
        organization_id=organization_id,
        user_id=None,
        filename="Scheduled Grants.gov pull",
        result=dict(result),
        processed_count=int(result.get("received", 0) or 0),
        created_count=int(result.get("created", 0) or 0),
        updated_count=int(result.get("updated", 0) or 0),
        unchanged_count=int(result.get("unchanged", 0) or 0),
        skipped_count=int(result.get("skipped", 0) or 0),
        error_count=int(result.get("errors", 0) or 0) + int(result.get("detail_errors", 0) or 0),
        reason_counts=reason_counts,
        reason_labels=reason_labels,
        notes=result.get("message"),
    )
    db.commit()
    return run


def _record_grants_failure_ingestion_run(db: Session, *, organization_id: int, error: Exception) -> IngestionRun:
    message = f"Scheduled Grants.gov pull failed: {sanitize_error_message(str(error))}"
    run = record_source_activity(
        db,
        source="grants.gov",
        organization_id=organization_id,
        user_id=None,
        filename="Scheduled Grants.gov pull",
        result={
            "status": "failed",
            "run_type": "Scheduled",
            "organization_id": organization_id,
            "received": 0,
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "skipped": 0,
            "errors": 1,
            "detail_errors": 0,
            "date_range_days": DEFAULT_GRANTS_POSTED_DAYS_BACK,
            "requested_date_window": f"{DEFAULT_GRANTS_POSTED_DAYS_BACK} days",
            "message": message,
        },
        processed_count=0,
        created_count=0,
        updated_count=0,
        unchanged_count=0,
        skipped_count=0,
        error_count=1,
        reason_counts={"scheduled_failure": 1},
        reason_labels={"scheduled_failure": message},
        notes=message,
    )
    db.commit()
    return run


def _grants_details(result: dict[str, Any], *, source_configs_processed: int, ingestion_run_id: int | None = None) -> dict[str, Any]:
    return {
        "source_configs_processed": source_configs_processed,
        "records_seen": int(result.get("received", 0) or 0),
        "created": int(result.get("created", 0) or 0),
        "updated": int(result.get("updated", 0) or 0),
        "unchanged": int(result.get("unchanged", 0) or 0),
        "skipped": int(result.get("skipped", 0) or 0),
        "errors": int(result.get("errors", 0) or 0) + int(result.get("detail_errors", 0) or 0),
        "detail_errors": int(result.get("detail_errors", 0) or 0),
        "pages_pulled": int(result.get("pages_pulled", 0) or 0),
        "requested_date_window": result.get("requested_date_window"),
        "ingestion_run_ids": [ingestion_run_id] if ingestion_run_id else [],
    }


def run_sam_ingest_job(
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    trigger_type: str = TRIGGER_TYPE_SCHEDULED,
) -> int:
    _print("SAM.gov ingestion job started")
    list_db = session_factory()
    try:
        config_rows = (
            list_db.query(SamSourceConfig.id, SamSourceConfig.organization_id)
            .join(Organization, Organization.id == SamSourceConfig.organization_id)
            .filter(Organization.is_live.is_(True))
            .order_by(SamSourceConfig.organization_id.asc(), SamSourceConfig.id.asc())
            .all()
        )
    finally:
        list_db.close()

    grouped: dict[int, list[int]] = defaultdict(list)
    for config_id, organization_id in config_rows:
        grouped[int(organization_id)].append(int(config_id))
    if not grouped:
        _print("No eligible SAM.gov source configurations found")
        _print("SAM.gov ingestion job finished")
        return 0

    status_counts: Counter[str] = Counter()
    for organization_id, config_ids in grouped.items():
        db = session_factory()
        job_run = None
        try:
            job_run = start_job_run(
                db,
                organization_id=organization_id,
                job_type=JOB_TYPE_SAM_INGEST,
                trigger_type=trigger_type,
                details={"source_config_ids": config_ids, "source_configs_processed": 0},
            )
            _print(f"Organization {organization_id}")
            _print(f"JobRun {job_run.id}")
            statuses: list[str] = []
            details_by_config: list[dict[str, Any]] = []
            for config_id in config_ids:
                try:
                    config = db.query(SamSourceConfig).filter(SamSourceConfig.id == config_id).one()
                    result = execute_sam_source_pull(
                        db,
                        organization_id=config.organization_id,
                        config=config,
                        run_type="Scheduled",
                        manual_pull=False,
                    )
                    statuses.append(_job_run_status(result.get("status")))
                    details_by_config.append(_sam_details(result, source_configs_processed=1))
                except Exception as exc:
                    db.rollback()
                    try:
                        failure_config = db.query(SamSourceConfig).filter(SamSourceConfig.id == config_id).first()
                        record_sam_failure_activity(
                            db,
                            organization_id=organization_id,
                            config=failure_config,
                            error=exc,
                            run_type="Scheduled",
                        )
                    except Exception:
                        db.rollback()
                    statuses.append(JOB_STATUS_FAILED)
                    details_by_config.append({
                        "source_configs_processed": 1,
                        "records_seen": 0,
                        "created": 0,
                        "updated": 0,
                        "unchanged": 0,
                        "skipped": 0,
                        "filtered": 0,
                        "errors": 1,
                        "pages_pulled": 0,
                        "search_requests_made": 0,
                        "checkpoint_saved": False,
                        "pause_reason": None,
                        "ingestion_run_ids": [],
                        "failed_source_config_id": config_id,
                    })
                    _print(f"Source config {config_id} failed: {type(exc).__name__}")
            status = _combine_statuses(statuses)
            details = _combine_sam_details(details_by_config)
            summary = (
                f"SAM.gov ingestion {status}: {details['records_seen']} records seen, "
                f"{details['created']} created, {details['updated']} updated, "
                f"{details['filtered']} filtered, {details['errors']} errors"
            )
            complete_job_run(db, job_run, status=status, summary=summary, details=details)
            status_counts[status] += 1
            _print(f"Status: {status}")
            _print(f"Records seen: {details['records_seen']}")
            _print(f"Created: {details['created']}")
            _print(f"Filtered: {details['filtered']}")
            if details.get("pause_reason"):
                _print(f"Reason: {details['pause_reason']}")
        except Exception as exc:
            db.rollback()
            if job_run is not None:
                fail_job_run(
                    db,
                    job_run,
                    exc,
                    summary="SAM.gov ingestion job failed before completion",
                    details={"source_config_ids": config_ids, "errors": 1},
                )
            status_counts[JOB_STATUS_FAILED] += 1
            _print(f"Organization {organization_id} failed: {type(exc).__name__}")
        finally:
            db.close()

    _print("SAM.gov ingestion job finished")
    _print(_job_summary(status_counts))
    return _job_exit_code(status_counts)


def run_grants_ingest_job(
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    trigger_type: str = TRIGGER_TYPE_SCHEDULED,
) -> int:
    _print("Grants.gov ingestion job started")
    list_db = session_factory()
    try:
        config_rows = (
            list_db.query(GrantsSourceConfig.id, GrantsSourceConfig.organization_id)
            .join(Organization, Organization.id == GrantsSourceConfig.organization_id)
            .filter(GrantsSourceConfig.enabled.is_(True))
            .filter(Organization.is_live.is_(True))
            .order_by(GrantsSourceConfig.organization_id.asc(), GrantsSourceConfig.id.asc())
            .all()
        )
    finally:
        list_db.close()

    grouped: dict[int, list[int]] = defaultdict(list)
    for config_id, organization_id in config_rows:
        grouped[int(organization_id)].append(int(config_id))
    if not grouped:
        _print("No enabled Grants.gov source configurations found")
        _print("Grants.gov ingestion job finished")
        return 0

    status_counts: Counter[str] = Counter()
    for organization_id, config_ids in grouped.items():
        db = session_factory()
        job_run = None
        try:
            job_run = start_job_run(
                db,
                organization_id=organization_id,
                job_type=JOB_TYPE_GRANTS_INGEST,
                trigger_type=trigger_type,
                details={"source_config_ids": config_ids, "source_configs_processed": 0},
            )
            _print(f"Organization {organization_id}")
            _print(f"JobRun {job_run.id}")
            config = db.query(GrantsSourceConfig).filter(GrantsSourceConfig.id == config_ids[0]).one()
            result = ingest_grants_gov(
                db,
                organization_id=config.organization_id,
                days_back=config.posted_days_back or DEFAULT_GRANTS_POSTED_DAYS_BACK,
                rows=config.rows or DEFAULT_GRANTS_ROWS,
                run_type="Scheduled",
            )
            ingestion_run = _record_grants_ingestion_run(db, organization_id=config.organization_id, result=result)
            status = _job_run_status(result.get("status"))
            details = _grants_details(
                result,
                source_configs_processed=len(config_ids),
                ingestion_run_id=ingestion_run.id,
            )
            summary = result.get("message") or (
                f"Grants.gov ingestion {status}: {details['records_seen']} records seen, "
                f"{details['created']} created, {details['updated']} updated, {details['errors']} errors"
            )
            complete_job_run(db, job_run, status=status, summary=summary, details=details)
            status_counts[status] += 1
            _print(f"Status: {status}")
            _print(f"Records seen: {details['records_seen']}")
            _print(f"Created: {details['created']}")
            _print(f"Updated: {details['updated']}")
        except Exception as exc:
            db.rollback()
            ingestion_run_id = None
            try:
                ingestion_run_id = _record_grants_failure_ingestion_run(
                    db,
                    organization_id=organization_id,
                    error=exc,
                ).id
            except Exception:
                db.rollback()
            if job_run is not None:
                fail_job_run(
                    db,
                    job_run,
                    exc,
                    summary="Grants.gov ingestion job failed",
                    details={
                        "source_configs_processed": len(config_ids),
                        "records_seen": 0,
                        "created": 0,
                        "updated": 0,
                        "unchanged": 0,
                        "skipped": 0,
                        "errors": 1,
                        "ingestion_run_ids": [ingestion_run_id] if ingestion_run_id else [],
                    },
                )
            status_counts[JOB_STATUS_FAILED] += 1
            _print(f"Organization {organization_id} failed: {type(exc).__name__}")
        finally:
            db.close()

    _print("Grants.gov ingestion job finished")
    _print(_job_summary(status_counts))
    return _job_exit_code(status_counts)


def _eligible_snapshot_users(db: Session, *, organization_id: int) -> list[User]:
    return (
        db.query(User)
        .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
        .filter(
            OrganizationMembership.organization_id == organization_id,
        )
        .order_by(User.id.asc())
        .all()
    )


def run_daily_snapshots_job(
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    trigger_type: str = TRIGGER_TYPE_SCHEDULED,
    snapshot_date: dt.date | None = None,
) -> int:
    snapshot_date = snapshot_date or dt.date.today()
    _print("Daily Snapshot job started")
    list_db = session_factory()
    try:
        workspace_rows = (
            list_db.query(Workspace.id, Workspace.organization_id)
            .join(Organization, Organization.id == Workspace.organization_id)
            .filter(Organization.is_active.is_(True))
            .order_by(Workspace.organization_id.asc())
            .all()
        )
    finally:
        list_db.close()

    if not workspace_rows:
        _print("No eligible workspaces found")
        _print("Daily Snapshot job finished")
        return 0

    status_counts: Counter[str] = Counter()
    for workspace_id, organization_id in workspace_rows:
        db = session_factory()
        job_run = None
        try:
            job_run = start_job_run(
                db,
                organization_id=int(organization_id),
                job_type=JOB_TYPE_DAILY_SNAPSHOT,
                trigger_type=trigger_type,
                details={"snapshot_date": snapshot_date.isoformat()},
            )
            _print(f"Organization {organization_id}")
            _print(f"JobRun {job_run.id}")
            users = _eligible_snapshot_users(db, organization_id=int(organization_id))
            details = {
                "snapshot_date": snapshot_date.isoformat(),
                "users_eligible": len(users),
                "snapshots_created": 0,
                "already_existed": 0,
                "failed": 0,
            }
            if not users:
                status = JOB_STATUS_SKIPPED
                summary = "Daily Snapshot skipped: no eligible users"
            else:
                for user in users:
                    try:
                        existing = (
                            db.query(DailySnapshot)
                            .filter(
                                DailySnapshot.workspace_id == int(workspace_id),
                                DailySnapshot.user_id == user.id,
                                DailySnapshot.snapshot_date == snapshot_date,
                            )
                            .first()
                        )
                        create_daily_snapshot(
                            db,
                            workspace_id=int(workspace_id),
                            user_id=user.id,
                            snapshot_date=snapshot_date,
                        )
                        if existing:
                            details["already_existed"] += 1
                        else:
                            details["snapshots_created"] += 1
                    except Exception:
                        db.rollback()
                        details["failed"] += 1
                if details["failed"] == 0:
                    status = JOB_STATUS_SUCCESS
                elif details["failed"] >= details["users_eligible"]:
                    status = JOB_STATUS_FAILED
                else:
                    status = JOB_STATUS_PARTIAL_SUCCESS
                summary = (
                    f"Daily Snapshot {status}: {details['snapshots_created']} created, "
                    f"{details['already_existed']} already existed, {details['failed']} failed"
                )
            complete_job_run(db, job_run, status=status, summary=summary, details=details)
            status_counts[status] += 1
            _print(f"Status: {status}")
            _print(f"Users eligible: {details['users_eligible']}")
            _print(f"Snapshots created: {details['snapshots_created']}")
            _print(f"Already existed: {details['already_existed']}")
            _print(f"Failed: {details['failed']}")
        except Exception as exc:
            db.rollback()
            if job_run is not None:
                fail_job_run(
                    db,
                    job_run,
                    exc,
                    summary="Daily Snapshot job failed before completion",
                    details={"snapshot_date": snapshot_date.isoformat(), "failed": 1},
                )
            status_counts[JOB_STATUS_FAILED] += 1
            _print(f"Organization {organization_id} failed: {type(exc).__name__}")
        finally:
            db.close()

    _print("Daily Snapshot job finished")
    _print(_job_summary(status_counts))
    return _job_exit_code(status_counts)


def run_daily_brief_emails_job(
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    trigger_type: str = TRIGGER_TYPE_SCHEDULED,
    snapshot_date: dt.date | None = None,
    email_sender: EmailSender | None = None,
) -> int:
    snapshot_date = snapshot_date or dt.date.today()
    sender = email_sender or ResendEmailSender()
    _print("Daily Brief Email job started")
    list_db = session_factory()
    try:
        workspace_rows = (
            list_db.query(Workspace.id, Workspace.organization_id)
            .join(Organization, Organization.id == Workspace.organization_id)
            .filter(Organization.is_active.is_(True))
            .filter(Organization.is_live.is_(True))
            .order_by(Workspace.organization_id.asc())
            .all()
        )
    finally:
        list_db.close()

    if not workspace_rows:
        _print("No eligible live workspaces found")
        _print("Daily Brief Email job finished")
        return 0

    status_counts: Counter[str] = Counter()
    for workspace_id, organization_id in workspace_rows:
        db = session_factory()
        job_run = None
        try:
            job_run = start_job_run(
                db,
                organization_id=int(organization_id),
                job_type=JOB_TYPE_DAILY_BRIEF_EMAIL,
                trigger_type=trigger_type,
                details={"snapshot_date": snapshot_date.isoformat()},
            )
            _print(f"Organization {organization_id}")
            _print(f"JobRun {job_run.id}")
            users = _eligible_snapshot_users(db, organization_id=int(organization_id))
            details = {
                "snapshot_date": snapshot_date.isoformat(),
                "users_eligible": len(users),
                "sent": 0,
                "skipped": 0,
                "failed": 0,
            }
            for user in users:
                email = (user.email or "").strip()
                if getattr(user, "daily_brief_email_opted_out", False):
                    details["skipped"] += 1
                    continue
                if not is_valid_recipient_email(email):
                    details["skipped"] += 1
                    continue

                existing = (
                    db.query(DailyBriefEmailDelivery)
                    .filter(
                        DailyBriefEmailDelivery.workspace_id == int(workspace_id),
                        DailyBriefEmailDelivery.user_id == user.id,
                        DailyBriefEmailDelivery.snapshot_date == snapshot_date,
                    )
                    .first()
                )
                if existing and existing.status == JOB_STATUS_SUCCESS:
                    details["skipped"] += 1
                    continue

                workspace = db.query(Workspace).filter(Workspace.id == int(workspace_id)).first()
                if workspace is None:
                    details["skipped"] += 1
                    continue
                message, item_count, skip_reason = build_daily_brief_email_message(
                    db,
                    workspace=workspace,
                    user_id=user.id,
                    user_name=user.name,
                    user_email=email,
                    snapshot_date=snapshot_date,
                )
                delivery = existing or DailyBriefEmailDelivery(
                    organization_id=int(organization_id),
                    workspace_id=int(workspace_id),
                    user_id=user.id,
                    snapshot_date=snapshot_date,
                    recipient_email=email,
                )
                delivery.recipient_email = email
                delivery.status = JOB_STATUS_RUNNING
                delivery.attempted_at = dt.datetime.now(dt.timezone.utc)
                delivery.item_count = item_count
                delivery.error_message = None
                if not existing:
                    db.add(delivery)
                db.commit()

                if not message:
                    delivery.status = JOB_STATUS_SKIPPED
                    delivery.error_message = skip_reason
                    db.commit()
                    details["skipped"] += 1
                    continue

                try:
                    result = sender.send(message)
                    delivery.status = JOB_STATUS_SUCCESS
                    delivery.sent_at = dt.datetime.now(dt.timezone.utc)
                    delivery.provider = result.provider
                    delivery.provider_message_id = result.message_id
                    delivery.error_message = None
                    db.commit()
                    details["sent"] += 1
                except Exception as exc:
                    db.rollback()
                    delivery = (
                        db.query(DailyBriefEmailDelivery)
                        .filter(
                            DailyBriefEmailDelivery.workspace_id == int(workspace_id),
                            DailyBriefEmailDelivery.user_id == user.id,
                            DailyBriefEmailDelivery.snapshot_date == snapshot_date,
                        )
                        .first()
                    )
                    if delivery is not None:
                        delivery.status = JOB_STATUS_FAILED
                        delivery.error_message = sanitize_error_message(str(exc))
                        delivery.provider = getattr(sender, "provider", None)
                        delivery.attempted_at = dt.datetime.now(dt.timezone.utc)
                        db.commit()
                    details["failed"] += 1

            if not users or (details["sent"] == 0 and details["failed"] == 0):
                status = JOB_STATUS_SKIPPED
            elif details["failed"] == 0:
                status = JOB_STATUS_SUCCESS
            elif details["sent"] == 0:
                status = JOB_STATUS_FAILED
            else:
                status = JOB_STATUS_PARTIAL_SUCCESS
            summary = (
                f"Daily Brief Email {status}: {details['sent']} sent, "
                f"{details['skipped']} skipped, {details['failed']} failed"
            )
            complete_job_run(db, job_run, status=status, summary=summary, details=details)
            status_counts[status] += 1
            _print(f"Status: {status}")
            _print(f"Sent: {details['sent']}")
            _print(f"Skipped: {details['skipped']}")
            _print(f"Failed: {details['failed']}")
        except Exception as exc:
            db.rollback()
            if job_run is not None:
                fail_job_run(db, job_run, exc, summary="Daily Brief Email job failed")
            status_counts[JOB_STATUS_FAILED] += 1
            _print(f"Status: {JOB_STATUS_FAILED}")
            _print(f"Error: {sanitize_error_message(str(exc))}")
        finally:
            db.close()

    _print("Daily Brief Email job finished")
    _print(_job_summary(status_counts))
    return _job_exit_code(status_counts)
