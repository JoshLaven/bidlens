from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from ..auth import attach_request_user_context, get_current_user
from ..database import get_db
from ..models import (
    IngestionRun,
    IngestionRunDetail,
    Opportunity,
    OpportunityUpdateEvent,
    OrganizationMembership,
    SamSourceConfig,
    User,
)
from ..services.ingestion_runs import record_source_activity
from ..services.market_activity import (
    MarketActivityFilters,
    build_market_activity,
    market_activity_filter_options,
)
from ..services.govwin_import import REASON_LABELS, import_govwin_xlsx
from ..services.opportunity_stages import normalize_display_stage
from ..services.sam_source_config import (
    SAM_NOTICE_TYPES,
    SamConfigValidationError,
    config_form_values,
    naics_catalog,
    validate_sam_config_input,
)
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
        "sam": "SAM.gov",
        "sam.gov": "SAM.gov",
        "grants_gov": "Grants.gov",
        "grants.gov": "Grants.gov",
        "govwin_export": "GovWin Upload",
        "govwin_api": "GovWin API",
    }
    return labels.get(source or "", source or "Source")


def _account_type_label(account_type: str | None) -> str:
    labels = {
        "Federal": "Federal",
        "State Government": "State Government",
        "Regional Government": "Regional Government",
        "Nonprofit University": "University",
        "__other__": "Other",
    }
    return labels.get(account_type or "", account_type or "Other")


def _parse_filter_date(value: str | None) -> date | None:
    if not value:
        return None


def _default_market_start(today: date) -> date:
    month_index = today.year * 12 + today.month - 12
    return date(month_index // 12, month_index % 12 + 1, 1)
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


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
    context["sam_config"] = (
        db.query(SamSourceConfig)
        .filter(SamSourceConfig.organization_id == org_id)
        .first()
    )
    return context


@router.get("/imports/govwin")
async def govwin_import_page(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    context = _intake_context(request, db, user)
    return templates.TemplateResponse("govwin_import.html", context)


def _sam_config_context(
    request: Request,
    db: Session,
    user,
    *,
    config: SamSourceConfig | None,
    form_values: dict | None = None,
    errors: dict[str, str] | None = None,
):
    org_id = _user_org_id(user)
    searches = (
        db.query(SamSourceConfig)
        .filter(SamSourceConfig.organization_id == org_id)
        .order_by(SamSourceConfig.name.asc(), SamSourceConfig.id.asc())
        .all()
    )
    latest_run = (
        db.query(IngestionRun)
        .filter(
            IngestionRun.organization_id == org_id,
            IngestionRun.source == "sam.gov",
        )
        .order_by(IngestionRun.started_at.desc(), IngestionRun.id.desc())
        .first()
    )
    return {
        "request": request,
        "user": user,
        "config": config,
        "searches": searches,
        "form_values": form_values or config_form_values(config),
        "errors": errors or {},
        "notice_type_options": SAM_NOTICE_TYPES,
        "naics_catalog": naics_catalog(),
        "latest_run": latest_run,
        "saved": request.query_params.get("saved") == "1",
        "active_page": "imports",
        "sidebar": get_sidebar(db, user),
    }


@router.get("/admin/sources/sam")
async def sam_source_config_page(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    org_id = _user_org_id(user)
    config = None
    if request.query_params.get("new") != "1":
        query = db.query(SamSourceConfig).filter(SamSourceConfig.organization_id == org_id)
        search_id = request.query_params.get("search_id")
        if search_id and search_id.isdigit():
            query = query.filter(SamSourceConfig.id == int(search_id))
        config = query.order_by(SamSourceConfig.name.asc(), SamSourceConfig.id.asc()).first()
    return templates.TemplateResponse(
        "sam_source_config.html",
        _sam_config_context(request, db, user, config=config),
    )


@router.post("/admin/sources/sam")
async def save_sam_source_config(
    request: Request,
    config_id: str = Form(""),
    search_name: str = Form(...),
    naics_codes: str = Form(...),
    keywords: str = Form(""),
    agencies: str = Form(""),
    set_asides: str = Form(""),
    notice_types: list[str] = Form(default=[]),
    posted_days_back: str = Form(...),
    due_days_from: str = Form(""),
    due_days_to: str = Form(""),
    active_only: str | None = Form(None),
    max_records: str = Form(...),
    db: Session = Depends(get_db),
):
    user = require_admin(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    org_id = _user_org_id(user)
    config = None
    if config_id.isdigit():
        config = (
            db.query(SamSourceConfig)
            .filter(
                SamSourceConfig.id == int(config_id),
                SamSourceConfig.organization_id == org_id,
            )
            .first()
        )
        if config is None:
            raise HTTPException(status_code=404, detail="Saved search not found.")
    raw_values = {
        "search_name": search_name,
        "naics_codes": naics_codes,
        "keywords": keywords,
        "agencies": agencies,
        "set_asides": set_asides,
        "notice_types": notice_types,
        "posted_days_back": posted_days_back,
        "due_days_from": due_days_from,
        "due_days_to": due_days_to,
        "active_only": active_only is not None,
        "max_records": max_records,
    }
    try:
        values = validate_sam_config_input(**raw_values)
        duplicate = (
            db.query(SamSourceConfig)
            .filter(
                SamSourceConfig.organization_id == org_id,
                SamSourceConfig.name == values["name"],
                SamSourceConfig.id != (config.id if config else -1),
            )
            .first()
        )
        if duplicate:
            raise SamConfigValidationError(
                {"search_name": "A saved search with this name already exists."}
            )
    except SamConfigValidationError as exc:
        return templates.TemplateResponse(
            "sam_source_config.html",
            _sam_config_context(
                request,
                db,
                user,
                config=config,
                form_values=raw_values,
                errors=exc.errors,
            ),
            status_code=422,
        )

    if config is None:
        config = SamSourceConfig(organization_id=org_id)
        db.add(config)
    for field_name, value in values.items():
        setattr(config, field_name, value)
    db.commit()

    org_id_param = request.query_params.get("org_id")
    query = urlencode({
        key: value
        for key, value in {
            "org_id": org_id_param,
            "saved": "1",
            "search_id": str(config.id),
        }.items()
        if value
    })
    return RedirectResponse(url=f"/admin/sources/sam?{query}", status_code=303)


@router.post("/admin/sources/sam/{search_id}/delete")
async def delete_sam_saved_search(
    search_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_admin(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    config = (
        db.query(SamSourceConfig)
        .filter(
            SamSourceConfig.id == search_id,
            SamSourceConfig.organization_id == _user_org_id(user),
        )
        .first()
    )
    if config is None:
        raise HTTPException(status_code=404, detail="Saved search not found.")
    db.delete(config)
    db.commit()
    org_id_param = request.query_params.get("org_id")
    suffix = f"?org_id={org_id_param}" if org_id_param and org_id_param.isdigit() else ""
    return RedirectResponse(url=f"/admin/sources/sam{suffix}", status_code=303)


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


@router.get("/imports/history/{run_id}")
async def import_history_detail_page(
    run_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_admin(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    run = (
        db.query(IngestionRun)
        .filter(
            IngestionRun.id == run_id,
            IngestionRun.organization_id == _user_org_id(user),
        )
        .first()
    )
    if not run:
        raise HTTPException(status_code=404, detail="Ingestion run not found")

    details = (
        db.query(IngestionRunDetail)
        .filter(IngestionRunDetail.ingestion_run_id == run.id)
        .order_by(IngestionRunDetail.id.asc())
        .all()
    )
    update_events = (
        db.query(OpportunityUpdateEvent)
        .options(joinedload(OpportunityUpdateEvent.opportunity))
        .filter(
            OpportunityUpdateEvent.organization_id == _user_org_id(user),
            OpportunityUpdateEvent.ingestion_run_id == run.id,
        )
        .order_by(OpportunityUpdateEvent.detected_at.asc(), OpportunityUpdateEvent.id.asc())
        .all()
    )
    return templates.TemplateResponse("import_history_detail.html", {
        "request": request,
        "user": user,
        "run": run,
        "details": details,
        "update_events": update_events,
        "source_label": _source_label,
        "active_page": "imports",
        "sidebar": get_sidebar(db, user),
    })


@router.get("/admin/source-updates")
async def source_update_log_page(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    source = (request.query_params.get("source") or "").strip()
    status = (request.query_params.get("status") or "").strip()
    sync_result = (request.query_params.get("result") or "").strip()
    date_from_value = (request.query_params.get("date_from") or "").strip()
    date_to_value = (request.query_params.get("date_to") or "").strip()
    date_from = _parse_filter_date(date_from_value)
    date_to = _parse_filter_date(date_to_value)
    try:
        page = max(1, int(request.query_params.get("page") or 1))
    except ValueError:
        page = 1
    per_page = 100

    query = (
        db.query(OpportunityUpdateEvent)
        .options(
            joinedload(OpportunityUpdateEvent.opportunity),
            joinedload(OpportunityUpdateEvent.ingestion_run),
        )
        .filter(OpportunityUpdateEvent.organization_id == _user_org_id(user))
    )
    if source:
        query = query.filter(OpportunityUpdateEvent.source == source)
    if status:
        query = query.filter(OpportunityUpdateEvent.salesforce_sync_status == status)
    if sync_result == "success":
        query = query.filter(OpportunityUpdateEvent.salesforce_sync_status == "succeeded")
    elif sync_result == "failed":
        query = query.filter(OpportunityUpdateEvent.salesforce_sync_status == "failed")
    elif sync_result == "not_attempted":
        query = query.filter(OpportunityUpdateEvent.salesforce_sync_status == "not_linked")
    if date_from:
        query = query.filter(
            OpportunityUpdateEvent.detected_at >= datetime.combine(date_from, time.min)
        )
    if date_to:
        query = query.filter(
            OpportunityUpdateEvent.detected_at < datetime.combine(date_to + timedelta(days=1), time.min)
        )

    total_events = query.count()
    total_pages = max(1, (total_events + per_page - 1) // per_page)
    page = min(page, total_pages)
    events = (
        query.order_by(
            OpportunityUpdateEvent.detected_at.desc(),
            OpportunityUpdateEvent.id.desc(),
        )
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    sources = [
        row[0]
        for row in (
            db.query(OpportunityUpdateEvent.source)
            .filter(OpportunityUpdateEvent.organization_id == _user_org_id(user))
            .distinct()
            .order_by(OpportunityUpdateEvent.source.asc())
            .all()
        )
    ]
    return templates.TemplateResponse("source_update_log.html", {
        "request": request,
        "user": user,
        "events": events,
        "sources": sources,
        "filters": {
            "source": source,
            "status": status,
            "result": sync_result,
            "date_from": date_from_value,
            "date_to": date_to_value,
        },
        "source_label": _source_label,
        "page": page,
        "total_pages": total_pages,
        "total_events": total_events,
        "filter_query": urlencode({
            key: value
            for key, value in {
                "source": source,
                "status": status,
                "result": sync_result,
                "date_from": date_from_value,
                "date_to": date_to_value,
            }.items()
            if value
        }),
        "active_page": "imports",
        "sidebar": get_sidebar(db, user),
    })


@router.get("/admin/source-updates/{event_id}")
async def source_update_detail_page(
    event_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_admin(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    event = (
        db.query(OpportunityUpdateEvent)
        .options(
            joinedload(OpportunityUpdateEvent.opportunity),
            joinedload(OpportunityUpdateEvent.ingestion_run),
        )
        .filter(
            OpportunityUpdateEvent.id == event_id,
            OpportunityUpdateEvent.organization_id == _user_org_id(user),
        )
        .first()
    )
    if not event:
        raise HTTPException(status_code=404, detail="Source update event not found")
    return templates.TemplateResponse("source_update_detail.html", {
        "request": request,
        "user": user,
        "event": event,
        "display_stage": normalize_display_stage(
            source=event.opportunity.source,
            opportunity_type=event.opportunity.opportunity_type,
            source_stage=event.opportunity.source_stage,
        ),
        "source_label": _source_label,
        "active_page": "imports",
        "sidebar": get_sidebar(db, user),
    })


@router.get("/admin/market-activity")
async def market_activity_page(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    today = date.today()
    start_date = _parse_filter_date(request.query_params.get("date_from")) or _default_market_start(today)
    end_date = _parse_filter_date(request.query_params.get("date_to")) or today
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    view = (request.query_params.get("view") or "overview").strip().lower()
    if view not in {"overview", "trends"}:
        view = "overview"

    filters = MarketActivityFilters(
        start_date=start_date,
        end_date=end_date,
        source=(request.query_params.get("source") or "").strip() or None,
        account_type=(request.query_params.get("account_type") or "").strip() or None,
        category=(request.query_params.get("category") or "").strip() or None,
        qualified_only=(request.query_params.get("qualified_only") or "") == "1",
        pushed_only=(request.query_params.get("pushed_only") or "") == "1",
    )
    organization_id = _user_org_id(user)
    dashboard = build_market_activity(
        db,
        organization_id=organization_id,
        filters=filters,
        today=today,
    )
    options = market_activity_filter_options(db, organization_id=organization_id)
    filter_query = urlencode({
        key: value
        for key, value in {
            "org_id": request.query_params.get("org_id"),
            "date_from": filters.start_date.isoformat(),
            "date_to": filters.end_date.isoformat(),
            "source": filters.source,
            "account_type": filters.account_type,
            "category": filters.category,
            "qualified_only": "1" if filters.qualified_only else None,
            "pushed_only": "1" if filters.pushed_only else None,
        }.items()
        if value
    })
    return templates.TemplateResponse("market_activity.html", {
        "request": request,
        "user": user,
        "dashboard": dashboard,
        "filters": filters,
        "options": options,
        "source_label": _source_label,
        "account_type_label": _account_type_label,
        "view": view,
        "filter_query": filter_query,
        "active_page": "imports",
        "sidebar": get_sidebar(db, user),
    })


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
