from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..auth import attach_request_user_context, get_current_user
from ..database import get_db
from ..models import IngestionRun, OrganizationMembership, User
from ..services.ingestion_runs import record_source_activity
from ..services.govwin_import import REASON_LABELS, import_govwin_xlsx
from .opportunities import get_sidebar

router = APIRouter()
templates = Jinja2Templates(directory="src/bidlens/templates")


def require_user(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None
    attach_request_user_context(request, db, user)
    return user


def _user_org_id(user) -> int:
    return getattr(user, "current_organization_id", None) or user.organization_id


def _current_user_role(db: Session, user) -> str:
    membership = (
        db.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == _user_org_id(user),
            OrganizationMembership.user_id == user.id,
        )
        .first()
    )
    return membership.role if membership else "member"


def require_admin(request: Request, db: Session):
    user = require_user(request, db)
    if not user:
        return None
    if getattr(user, "current_role", "member") != "admin":
        raise HTTPException(status_code=403, detail="Only organization admins can view Source Activity.")
    return user


def _context(request: Request, user, result=None, error: str | None = None):
    return {
        "request": request,
        "user": user,
        "result": result,
        "error": error,
        "active_page": "imports",
    }


def _record_govwin_import_run(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    filename: str,
    result: dict | None = None,
    error_reason: str | None = None,
    error_message: str | None = None,
) -> IngestionRun:
    reason_counts = dict((result or {}).get("reason_counts") or {})
    if error_reason:
        reason_counts[error_reason] = reason_counts.get(error_reason, 0) + 1
    reason_labels = {**REASON_LABELS, **{
        "invalid_file_type": "Invalid file type",
        "empty_file": "Empty file",
        "import_error": "Import error",
    }}

    return record_source_activity(
        db,
        source="govwin_export",
        organization_id=organization_id,
        user_id=user_id,
        filename=filename or None,
        result=result,
        error_count=1 if error_reason else None,
        reason_counts=reason_counts,
        reason_labels=reason_labels,
        notes=error_message,
    )


def _reason_summary_items(run: IngestionRun) -> list[dict]:
    summary = run.reason_summary_json if isinstance(run.reason_summary_json, dict) else {}
    reason_counts = summary.get("reason_counts") if isinstance(summary.get("reason_counts"), dict) else {}
    reason_labels = summary.get("reason_labels") if isinstance(summary.get("reason_labels"), dict) else {}
    items = []
    for reason_code, count in sorted(reason_counts.items()):
        items.append({
            "code": reason_code,
            "label": reason_labels.get(reason_code) or REASON_LABELS.get(reason_code) or reason_code,
            "count": count,
        })
    return items


def _source_label(source: str | None) -> str:
    labels = {
        "sam.gov": "SAM.gov",
        "grants.gov": "Grants.gov",
        "govwin_export": "GovWin Upload",
        "govwin_api": "GovWin API",
    }
    return labels.get(source or "", source or "Source")


def _activity_status(run: IngestionRun) -> str:
    return "Error" if (run.error_count or 0) else "Success"


def _activity_summary(run: IngestionRun) -> str:
    parts = []
    if run.created_count:
        parts.append(f"created {run.created_count}")
    if run.updated_count:
        parts.append(f"updated {run.updated_count}")
    if run.unchanged_count:
        parts.append(f"unchanged {run.unchanged_count}")
    if run.skipped_count:
        parts.append(f"skipped {run.skipped_count}")
    if run.error_count:
        parts.append(f"errors {run.error_count}")
    if not parts and run.processed_count:
        parts.append(f"processed {run.processed_count}")
    return " · ".join(parts) or (run.notes or "Completed")


def _recent_activity(db: Session, org_id: int, limit: int = 5) -> list[dict]:
    runs = (
        db.query(IngestionRun)
        .filter(IngestionRun.organization_id == org_id)
        .order_by(IngestionRun.started_at.desc(), IngestionRun.id.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "label": _source_label(run.source),
            "status": _activity_status(run),
            "summary": _activity_summary(run),
            "timestamp": run.finished_at or run.started_at,
        }
        for run in runs
    ]


def _latest_runs_by_source(db: Session, org_id: int) -> dict[str, IngestionRun]:
    latest = {}
    for source in ("sam.gov", "grants.gov", "govwin_export"):
        latest[source] = (
            db.query(IngestionRun)
            .filter(
                IngestionRun.organization_id == org_id,
                IngestionRun.source == source,
            )
            .order_by(IngestionRun.started_at.desc(), IngestionRun.id.desc())
            .first()
        )
    return latest


def _intake_context(request: Request, db: Session, user, result=None, error: str | None = None):
    org_id = _user_org_id(user)
    context = _context(request, user, result=result, error=error)
    context["sidebar"] = get_sidebar(db, user)
    context["latest_runs"] = _latest_runs_by_source(db, org_id)
    context["recent_activity"] = _recent_activity(db, org_id)
    return context


@router.get("/imports/govwin")
async def govwin_import_page(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    context = _intake_context(request, db, user)
    return templates.TemplateResponse("govwin_import.html", context)


async def _source_activity_response(request: Request, db: Session):
    user = require_admin(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    org_id = _user_org_id(user)
    rows = (
        db.query(IngestionRun, User)
        .outerjoin(User, User.id == IngestionRun.user_id)
        .filter(IngestionRun.organization_id == org_id)
        .order_by(IngestionRun.started_at.desc(), IngestionRun.id.desc())
        .limit(50)
        .all()
    )
    runs = [
        {
            "run": run,
            "user_email": run_user.email if run_user else "",
            "reason_summary_items": _reason_summary_items(run),
        }
        for run, run_user in rows
    ]
    return templates.TemplateResponse("import_history.html", {
        "request": request,
        "user": user,
        "runs": runs,
        "active_page": "imports",
        "sidebar": get_sidebar(db, user),
    })


@router.get("/imports/history")
async def import_history_page(request: Request, db: Session = Depends(get_db)):
    return await _source_activity_response(request, db)


@router.get("/source-activity")
async def source_activity_page(request: Request, db: Session = Depends(get_db)):
    return await _source_activity_response(request, db)


@router.post("/imports/govwin")
async def govwin_import_upload(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    error = None
    result = None
    filename = file.filename or ""
    org_id = _user_org_id(user)
    if not filename.lower().endswith(".xlsx"):
        error = "Upload a GovWin .xlsx export."
        _record_govwin_import_run(
            db,
            organization_id=org_id,
            user_id=user.id,
            filename=filename,
            error_reason="invalid_file_type",
            error_message=error,
        )
        db.commit()
    else:
        try:
            file_bytes = await file.read()
            if not file_bytes:
                error = "The uploaded file was empty."
                _record_govwin_import_run(
                    db,
                    organization_id=org_id,
                    user_id=user.id,
                    filename=filename,
                    error_reason="empty_file",
                    error_message=error,
                )
                db.commit()
            else:
                result = import_govwin_xlsx(db, org_id, file_bytes)
                _record_govwin_import_run(
                    db,
                    organization_id=org_id,
                    user_id=user.id,
                    filename=filename,
                    result=result,
                )
                db.commit()
        except Exception as exc:
            db.rollback()
            error = f"Unable to import GovWin export: {exc}"
            _record_govwin_import_run(
                db,
                organization_id=org_id,
                user_id=user.id,
                filename=filename,
                error_reason="import_error",
                error_message=error,
            )
            db.commit()

    context = _intake_context(request, db, user, result=result, error=error)
    return templates.TemplateResponse("govwin_import.html", context)
