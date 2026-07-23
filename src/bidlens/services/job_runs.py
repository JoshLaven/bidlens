from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

from sqlalchemy.orm import Session

from ..models import JobRun


JOB_TYPE_SAM_INGEST = "sam_ingest"
JOB_TYPE_GRANTS_INGEST = "grants_ingest"
JOB_TYPE_DAILY_SNAPSHOT = "daily_snapshot"
JOB_TYPE_DAILY_BRIEF_EMAIL = "daily_brief_email"

TRIGGER_TYPE_SCHEDULED = "scheduled"
TRIGGER_TYPE_MANUAL = "manual"
TRIGGER_TYPE_RETRY = "retry"
TRIGGER_TYPE_SYSTEM = "system"

JOB_STATUS_RUNNING = "running"
JOB_STATUS_SUCCESS = "success"
JOB_STATUS_PARTIAL_SUCCESS = "partial_success"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_PAUSED = "paused"
JOB_STATUS_SKIPPED = "skipped"

TERMINAL_JOB_STATUSES = {
    JOB_STATUS_SUCCESS,
    JOB_STATUS_PARTIAL_SUCCESS,
    JOB_STATUS_FAILED,
    JOB_STATUS_PAUSED,
    JOB_STATUS_SKIPPED,
}

MAX_ERROR_MESSAGE_LENGTH = 500
MAX_ERROR_TYPE_LENGTH = 120


_SECRET_PATTERNS = [
    (re.compile(r"(?i)(bearer\s+)[a-z0-9._~+/=-]+"), r"\1[redacted]"),
    (re.compile(r"(?i)(api[_-]?key|token|secret|password)(\s*[=:]\s*)[^\s,;&]+"), r"\1\2[redacted]"),
    (re.compile(r"(?i)([\"'](?:api[_-]?key|token|secret|password)[\"']\s*:\s*[\"'])[^\"']+([\"'])"), r"\1[redacted]\2"),
    (re.compile(r"([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^@\s/]+)@", re.IGNORECASE), r"\1[redacted]:[redacted]@"),
    (re.compile(r"\bsk-[a-zA-Z0-9_-]{10,}\b"), "sk-[redacted]"),
]


def sanitize_error_message(message: str | None) -> str | None:
    if not message:
        return None
    safe_message = str(message)
    for pattern, replacement in _SECRET_PATTERNS:
        safe_message = pattern.sub(replacement, safe_message)
    if len(safe_message) > MAX_ERROR_MESSAGE_LENGTH:
        safe_message = safe_message[: MAX_ERROR_MESSAGE_LENGTH - 3].rstrip() + "..."
    return safe_message


def _duration_ms(started_at: datetime | None, finished_at: datetime | None) -> int | None:
    if not started_at or not finished_at:
        return None
    if started_at.tzinfo is None and finished_at.tzinfo is not None:
        finished_at = finished_at.replace(tzinfo=None)
    elif started_at.tzinfo is not None and finished_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=None)
    try:
        return max(0, int((finished_at - started_at).total_seconds() * 1000))
    except TypeError:
        return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _commit_and_refresh(db: Session, run: JobRun) -> JobRun:
    db.commit()
    db.refresh(run)
    return run


def start_job_run(
    db: Session,
    *,
    organization_id: int,
    job_type: str,
    trigger_type: str = TRIGGER_TYPE_SYSTEM,
    scheduled_for: datetime | None = None,
    summary: str | None = None,
    details: dict[str, Any] | None = None,
) -> JobRun:
    run = JobRun(
        organization_id=organization_id,
        job_type=job_type,
        trigger_type=trigger_type,
        status=JOB_STATUS_RUNNING,
        scheduled_for=scheduled_for,
        started_at=_utcnow(),
        summary=summary,
        details_json=dict(details or {}),
    )
    db.add(run)
    return _commit_and_refresh(db, run)


def complete_job_run(
    db: Session,
    run: JobRun,
    *,
    status: str = JOB_STATUS_SUCCESS,
    summary: str | None = None,
    details: dict[str, Any] | None = None,
) -> JobRun:
    if status not in TERMINAL_JOB_STATUSES:
        raise ValueError(f"Job run completion status must be terminal, got {status!r}")

    finished_at = _utcnow()
    run.status = status
    run.finished_at = finished_at
    run.duration_ms = _duration_ms(run.started_at, finished_at)
    if summary is not None:
        run.summary = summary
    if details is not None:
        run.details_json = dict(details)
    run.error_type = None
    run.error_message = None
    return _commit_and_refresh(db, run)


def fail_job_run(
    db: Session,
    run: JobRun,
    error: BaseException | str,
    *,
    summary: str | None = None,
    details: dict[str, Any] | None = None,
) -> JobRun:
    finished_at = _utcnow()
    if isinstance(error, BaseException):
        error_type = type(error).__name__
        error_message = str(error)
    else:
        error_type = "JobRunFailure"
        error_message = str(error)

    run.status = JOB_STATUS_FAILED
    run.finished_at = finished_at
    run.duration_ms = _duration_ms(run.started_at, finished_at)
    run.summary = summary if summary is not None else run.summary
    if details is not None:
        run.details_json = dict(details)
    run.error_type = error_type[:MAX_ERROR_TYPE_LENGTH]
    run.error_message = sanitize_error_message(error_message)
    return _commit_and_refresh(db, run)


def safely_fail_job_run(
    db: Session,
    run: JobRun | None,
    error: BaseException | str,
    *,
    summary: str | None = None,
    details: dict[str, Any] | None = None,
) -> JobRun | None:
    if run is None:
        return None
    try:
        return fail_job_run(db, run, error, summary=summary, details=details)
    except Exception:
        db.rollback()
        return None
