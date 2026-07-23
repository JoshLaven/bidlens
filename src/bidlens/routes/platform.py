from __future__ import annotations

import datetime as dt
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..auth import create_session, get_current_user, is_platform_admin_email
from ..database import get_db
from ..models import JobRun, Organization, OrganizationMembership, Plan, User, Workspace, WorkspaceInvitation
from ..services.platform import (
    PROFESSIONAL_PLAN_CODE,
    accept_workspace_invitation,
    create_owner_replacement_invitation,
    create_replacement_workspace_invitation,
    delete_test_organization,
    invitation_url,
    is_protected_platform_organization,
    platform_plan_definitions,
    provision_workspace,
    ProvisionWorkspaceInput,
    post_invitation_acceptance_url,
)
from ..tenancy import duplicate_domain_diagnostics


router = APIRouter()
templates = Jinja2Templates(directory="src/bidlens/templates")
OPERATIONS_PAGE_SIZE = 25

JOB_LABELS = {
    "sam_ingest": "SAM.gov Pull",
    "grants_ingest": "Grants.gov Pull",
    "daily_snapshot": "Daily Snapshot",
    "daily_brief_email": "Daily Brief Email",
    "govwin_ingest": "GovWin Pull",
    "outlook_classification": "Outlook Classification",
}
TRIGGER_LABELS = {
    "scheduled": "Scheduled",
    "manual": "Manual",
    "retry": "Retry",
    "system": "System",
}
STATUS_LABELS = {
    "running": "Running",
    "success": "Success",
    "partial_success": "Partial Success",
    "failed": "Failed",
    "paused": "Paused",
    "skipped": "Skipped",
}
DETAIL_LABELS = {
    "source_configs_processed": "Source configs processed",
    "records_seen": "Records seen",
    "created": "Created",
    "updated": "Updated",
    "unchanged": "Unchanged",
    "skipped": "Skipped",
    "filtered": "Filtered",
    "errors": "Errors",
    "detail_errors": "Detail errors",
    "pages_pulled": "Pages pulled",
    "search_requests_made": "Search requests made",
    "checkpoint_saved": "Checkpoint saved",
    "pause_reason": "Pause reason",
    "users_eligible": "Users eligible",
    "sent": "Sent",
    "snapshots_created": "Snapshots created",
    "already_existed": "Already existed",
    "failed": "Failed",
    "requested_date_window": "Requested date window",
    "snapshot_date": "Snapshot date",
}
DETAIL_ORDER = (
    "source_configs_processed",
    "records_seen",
    "created",
    "updated",
    "unchanged",
    "skipped",
    "filtered",
    "errors",
    "detail_errors",
    "pages_pulled",
    "search_requests_made",
    "checkpoint_saved",
    "pause_reason",
    "users_eligible",
    "snapshots_created",
    "already_existed",
    "failed",
    "requested_date_window",
    "snapshot_date",
)
SENSITIVE_DETAIL_KEYS = ("secret", "token", "password", "api_key", "apikey", "credential", "database_url")


def is_platform_admin(user) -> bool:
    return bool(user and is_platform_admin_email(getattr(user, "email", "")))


def require_platform_admin(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None
    setattr(user, "is_platform_admin", is_platform_admin(user))
    if not is_platform_admin(user):
        raise HTTPException(status_code=404, detail="Not found")
    return user


def _label(mapping: dict[str, str], value: str | None) -> str:
    if not value:
        return "Unknown"
    return mapping.get(value, str(value).replace("_", " ").title())


def _format_datetime(value: dt.datetime | None) -> str:
    if not value:
        return "—"
    return value.strftime("%b %-d, %-I:%M %p")


def _format_duration(run: JobRun) -> str:
    if run.duration_ms is not None:
        seconds = max(0, run.duration_ms / 1000)
    elif run.started_at and run.finished_at:
        seconds = max(0, (run.finished_at - run.started_at).total_seconds())
    else:
        return "—"
    if seconds < 1:
        return f"{int(seconds * 1000)}ms"
    if seconds < 60:
        return f"{seconds:.1f}s" if seconds < 10 else f"{int(seconds)}s"
    minutes = int(seconds // 60)
    remaining = int(seconds % 60)
    return f"{minutes}m {remaining}s"


def _format_detail_value(value):
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) if value else "—"
    if value is None or value == "":
        return "—"
    return str(value).replace("_", " ").title() if isinstance(value, str) and value.islower() and "_" in value else str(value)


def _safe_detail_items(details: dict | None) -> list[dict[str, str]]:
    details = dict(details or {})
    keys = [key for key in DETAIL_ORDER if key in details]
    keys.extend(sorted(key for key in details.keys() if key not in keys))
    items = []
    for key in keys:
        lowered = str(key).lower()
        if any(marker in lowered for marker in SENSITIVE_DETAIL_KEYS):
            continue
        value = details.get(key)
        if value is None or value == "" or value == []:
            continue
        items.append({
            "key": key,
            "label": DETAIL_LABELS.get(key, str(key).replace("_", " ").title()),
            "value": _format_detail_value(value),
        })
    return items


def _organization_display(org: Organization | None, workspace: Workspace | None) -> str:
    if workspace and workspace.name:
        return workspace.name
    if org and org.name:
        return org.name
    return "Unknown workspace"


def _operation_row(run: JobRun, org: Organization | None, workspace: Workspace | None) -> dict:
    return {
        "run": run,
        "organization_name": _organization_display(org, workspace),
        "organization_id": run.organization_id,
        "job_label": _label(JOB_LABELS, run.job_type),
        "trigger_label": _label(TRIGGER_LABELS, run.trigger_type),
        "status_label": _label(STATUS_LABELS, run.status),
        "status_class": str(run.status or "unknown").replace("_", "-"),
        "started_label": _format_datetime(run.started_at),
        "finished_label": _format_datetime(run.finished_at),
        "scheduled_for_label": _format_datetime(run.scheduled_for),
        "duration_label": _format_duration(run),
        "summary": run.summary or "—",
        "details": _safe_detail_items(run.details_json),
    }


def _parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _operation_filter_options(db: Session) -> dict:
    organizations = (
        db.query(Organization)
        .filter(or_(Organization.plan.is_(None), Organization.plan != "platform"))
        .order_by(Organization.name.asc(), Organization.id.asc())
        .all()
    )
    return {
        "organizations": organizations,
        "job_types": [{"value": key, "label": value} for key, value in JOB_LABELS.items() if key in {"sam_ingest", "grants_ingest", "daily_snapshot", "daily_brief_email"}],
        "statuses": [{"value": key, "label": value} for key, value in STATUS_LABELS.items()],
    }


def _operations_context(request: Request, db: Session, user: User) -> dict:
    params = request.query_params
    organization_id = params.get("organization_id") or ""
    job_type = params.get("job_type") or ""
    status = params.get("status") or ""
    date_from = params.get("date_from") or ""
    date_to = params.get("date_to") or ""
    page = max(1, int(params.get("page") or 1) if str(params.get("page") or "1").isdigit() else 1)

    query = (
        db.query(JobRun, Organization, Workspace)
        .join(Organization, Organization.id == JobRun.organization_id)
        .outerjoin(Workspace, Workspace.organization_id == Organization.id)
    )
    if organization_id.isdigit():
        query = query.filter(JobRun.organization_id == int(organization_id))
    if job_type:
        query = query.filter(JobRun.job_type == job_type)
    if status:
        query = query.filter(JobRun.status == status)
    parsed_from = _parse_date(date_from)
    if parsed_from:
        query = query.filter(JobRun.started_at >= dt.datetime.combine(parsed_from, dt.time.min))
    parsed_to = _parse_date(date_to)
    if parsed_to:
        query = query.filter(JobRun.started_at < dt.datetime.combine(parsed_to + dt.timedelta(days=1), dt.time.min))

    total_count = query.count()
    total_pages = max(1, (total_count + OPERATIONS_PAGE_SIZE - 1) // OPERATIONS_PAGE_SIZE)
    if page > total_pages:
        page = total_pages
    rows = (
        query
        .order_by(JobRun.started_at.desc(), JobRun.id.desc())
        .offset((page - 1) * OPERATIONS_PAGE_SIZE)
        .limit(OPERATIONS_PAGE_SIZE)
        .all()
    )
    base_filters = {
        "organization_id": organization_id,
        "job_type": job_type,
        "status": status,
        "date_from": date_from,
        "date_to": date_to,
    }
    prev_query = urlencode({**base_filters, "page": page - 1}) if page > 1 else None
    next_query = urlencode({**base_filters, "page": page + 1}) if page < total_pages else None
    has_filters = any(base_filters.values())
    return {
        "request": request,
        "user": user,
        "active_page": "platform_operations",
        "filters": base_filters,
        "filter_options": _operation_filter_options(db),
        "runs": [_operation_row(run, org, workspace) for run, org, workspace in rows],
        "total_count": total_count,
        "page": page,
        "page_size": OPERATIONS_PAGE_SIZE,
        "total_pages": total_pages,
        "prev_url": f"/platform/operations?{prev_query}" if prev_query else None,
        "next_url": f"/platform/operations?{next_query}" if next_query else None,
        "has_filters": has_filters,
    }


def _operation_detail_context(request: Request, db: Session, user: User, run_id: int) -> dict:
    row = (
        db.query(JobRun, Organization, Workspace)
        .join(Organization, Organization.id == JobRun.organization_id)
        .outerjoin(Workspace, Workspace.organization_id == Organization.id)
        .filter(JobRun.id == run_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Job run not found")
    run, org, workspace = row
    return {
        "request": request,
        "user": user,
        "active_page": "platform_operations",
        "row": _operation_row(run, org, workspace),
    }


def _organization_rows(db: Session) -> list[dict]:
    rows = (
        db.query(Organization)
        .filter(or_(Organization.plan.is_(None), Organization.plan != "platform"))
        .order_by(Organization.created_at.desc(), Organization.id.desc())
        .all()
    )
    results = []
    for org in rows:
        workspace = (
            db.query(Workspace)
            .filter(Workspace.organization_id == org.id)
            .first()
        )
        plan = workspace.plan if workspace and workspace.plan else db.query(Plan).filter(Plan.code == org.plan).first()
        owner_invite = (
            db.query(WorkspaceInvitation)
            .filter(WorkspaceInvitation.organization_id == org.id)
            .order_by(WorkspaceInvitation.created_at.desc(), WorkspaceInvitation.id.desc())
            .first()
        )
        pending_count = (
            db.query(WorkspaceInvitation)
            .filter(
                WorkspaceInvitation.organization_id == org.id,
                WorkspaceInvitation.status == "pending",
            )
            .count()
        )
        results.append({
            "organization": org,
            "workspace": workspace,
            "plan": plan,
            "latest_invitation": owner_invite,
            "pending_invitations": pending_count,
        })
    return results


def _member_rows(db: Session, organization_id: int) -> list[dict]:
    rows = (
        db.query(OrganizationMembership, User)
        .join(User, User.id == OrganizationMembership.user_id)
        .filter(OrganizationMembership.organization_id == organization_id)
        .order_by(OrganizationMembership.role.asc(), User.email.asc())
        .all()
    )
    return [
        {
            "membership": membership,
            "user": member,
            "status": "active",
        }
        for membership, member in rows
    ]


def _invitation_rows(request: Request, db: Session, organization_id: int) -> list[dict]:
    invitations = (
        db.query(WorkspaceInvitation)
        .filter(WorkspaceInvitation.organization_id == organization_id)
        .order_by(WorkspaceInvitation.status.asc(), WorkspaceInvitation.created_at.desc(), WorkspaceInvitation.id.desc())
        .all()
    )
    base_url = str(request.base_url).rstrip("/")
    return [
        {
            "invitation": invitation,
            "url": invitation_url(base_url, invitation),
            "expires_label": "—",
        }
        for invitation in invitations
    ]


def _owner_state(db: Session, organization_id: int) -> dict:
    owner_invitation = (
        db.query(WorkspaceInvitation)
        .filter(
            WorkspaceInvitation.organization_id == organization_id,
            WorkspaceInvitation.role == "admin",
        )
        .order_by(WorkspaceInvitation.created_at.desc(), WorkspaceInvitation.id.desc())
        .first()
    )
    owner_user = None
    owner_membership = None
    if owner_invitation:
        owner_user = db.query(User).filter(User.email == owner_invitation.email).first()
        if owner_user:
            owner_membership = (
                db.query(OrganizationMembership)
                .filter(
                    OrganizationMembership.organization_id == organization_id,
                    OrganizationMembership.user_id == owner_user.id,
                )
                .first()
            )

    if not owner_membership:
        owner_membership = (
            db.query(OrganizationMembership)
            .join(User, User.id == OrganizationMembership.user_id)
            .filter(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.role == "admin",
            )
            .order_by(OrganizationMembership.id.asc())
            .first()
        )
        if owner_membership:
            owner_user = owner_membership.user

    if owner_invitation and owner_invitation.status == "pending":
        status = "Invitation pending"
    elif owner_membership:
        status = "Accepted and active"
    else:
        status = "No usable invitation"

    return {
        "user": owner_user,
        "membership": owner_membership,
        "invitation": owner_invitation,
        "status": status,
        "can_create_replacement": not owner_membership and not (owner_invitation and owner_invitation.status == "pending"),
    }


def _organization_detail_context(
    request: Request,
    db: Session,
    user: User,
    organization_id: int,
    *,
    message: str | None = None,
    error: str | None = None,
) -> dict:
    org = db.get(Organization, organization_id)
    if not org or is_protected_platform_organization(org):
        raise HTTPException(status_code=404, detail="Organization not found")
    workspace = (
        db.query(Workspace)
        .filter(Workspace.organization_id == org.id)
        .first()
    )
    plan = workspace.plan if workspace and workspace.plan else db.query(Plan).filter(Plan.code == org.plan).first()
    members = _member_rows(db, org.id)
    invitations = _invitation_rows(request, db, org.id)
    pending_invitations = [row for row in invitations if row["invitation"].status == "pending"]
    return {
        "request": request,
        "user": user,
        "active_page": "platform",
        "organization": org,
        "workspace": workspace,
        "plan": plan,
        "owner_state": _owner_state(db, org.id),
        "members": members,
        "active_members": members,
        "pending_members": pending_invitations,
        "invitations": invitations,
        "pending_invitations": pending_invitations,
        "message": message,
        "error": error,
    }


@router.get("/platform")
async def platform_page(request: Request, db: Session = Depends(get_db)):
    user = require_platform_admin(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    return templates.TemplateResponse("platform.html", {
        "request": request,
        "user": user,
        "active_page": "platform",
        "organizations": _organization_rows(db),
        "plans": platform_plan_definitions(),
        "selected_plan": PROFESSIONAL_PLAN_CODE,
        "provisioned": None,
        "form": {},
        "error": None,
    })


@router.get("/platform/operations")
async def platform_operations_page(request: Request, db: Session = Depends(get_db)):
    user = require_platform_admin(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse("platform_operations.html", _operations_context(request, db, user))


@router.get("/platform/operations/{run_id}")
async def platform_operation_detail(run_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_platform_admin(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse("platform_operation_detail.html", _operation_detail_context(request, db, user, run_id))


@router.get("/platform/organizations/{organization_id}")
async def platform_organization_detail(organization_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_platform_admin(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(
        "platform_organization_detail.html",
        _organization_detail_context(request, db, user, organization_id),
    )


@router.post("/platform/organizations/{organization_id}/invitations/{invitation_id}/replace")
async def platform_replace_invitation(
    organization_id: int,
    invitation_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_platform_admin(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    invitation = (
        db.query(WorkspaceInvitation)
        .filter(
            WorkspaceInvitation.id == invitation_id,
            WorkspaceInvitation.organization_id == organization_id,
        )
        .first()
    )
    if not invitation:
        raise HTTPException(status_code=404, detail="Invitation not found")
    try:
        create_replacement_workspace_invitation(db, invitation=invitation, platform_user_id=user.id)
        return RedirectResponse(url=f"/platform/organizations/{organization_id}?recovered=1", status_code=303)
    except ValueError as exc:
        return templates.TemplateResponse(
            "platform_organization_detail.html",
            _organization_detail_context(request, db, user, organization_id, error=str(exc)),
            status_code=400,
        )


@router.post("/platform/organizations/{organization_id}/owner-invitation")
async def platform_create_owner_invitation(
    organization_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_platform_admin(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    try:
        create_owner_replacement_invitation(db, organization_id=organization_id, platform_user_id=user.id)
        return RedirectResponse(url=f"/platform/organizations/{organization_id}?recovered=1", status_code=303)
    except ValueError as exc:
        return templates.TemplateResponse(
            "platform_organization_detail.html",
            _organization_detail_context(request, db, user, organization_id, error=str(exc)),
            status_code=400,
        )


@router.get("/platform/organizations/{organization_id}/delete")
async def platform_delete_organization_confirm(organization_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_platform_admin(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    org = db.get(Organization, organization_id)
    if not org or is_protected_platform_organization(org):
        raise HTTPException(status_code=404, detail="Organization not found")
    return templates.TemplateResponse("platform_organization_delete.html", {
        "request": request,
        "user": user,
        "active_page": "platform",
        "organization": org,
        "error": None,
    })


@router.post("/platform/organizations/{organization_id}/delete")
async def platform_delete_organization(
    organization_id: int,
    request: Request,
    confirmation_name: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_platform_admin(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    org = db.get(Organization, organization_id)
    if not org or is_protected_platform_organization(org):
        raise HTTPException(status_code=404, detail="Organization not found")
    try:
        delete_test_organization(
            db,
            organization_id=organization_id,
            confirmation_name=confirmation_name,
            platform_admin_user_id=user.id,
        )
        return RedirectResponse(url="/platform?deleted=1", status_code=303)
    except ValueError as exc:
        return templates.TemplateResponse("platform_organization_delete.html", {
            "request": request,
            "user": user,
            "active_page": "platform",
            "organization": org,
            "error": str(exc),
        }, status_code=400)


@router.get("/platform/diagnostics/duplicate-domains")
async def platform_duplicate_domain_diagnostics(request: Request, db: Session = Depends(get_db)):
    user = require_platform_admin(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return JSONResponse({
        "duplicate_domains": duplicate_domain_diagnostics(db),
    })


@router.post("/platform/organizations")
async def platform_create_organization(
    request: Request,
    organization_name: str = Form(""),
    owner_name: str = Form(""),
    owner_email: str = Form(""),
    operational_contact_is_owner: str | None = Form(None),
    operational_contact_name: str = Form(""),
    operational_contact_email: str = Form(""),
    billing_contact_name: str = Form(""),
    billing_contact_email: str = Form(""),
    plan_code: str = Form(PROFESSIONAL_PLAN_CODE),
    db: Session = Depends(get_db),
):
    user = require_platform_admin(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    form = {
        "organization_name": organization_name,
        "owner_name": owner_name,
        "owner_email": owner_email,
        "operational_contact_is_owner": bool(operational_contact_is_owner),
        "operational_contact_name": operational_contact_name,
        "operational_contact_email": operational_contact_email,
        "billing_contact_name": billing_contact_name,
        "billing_contact_email": billing_contact_email,
        "plan_code": plan_code,
    }
    try:
        provisioned = provision_workspace(
            db,
            payload=ProvisionWorkspaceInput(
                organization_name=organization_name,
                owner_name=owner_name,
                owner_email=owner_email,
                operational_contact_is_owner=bool(operational_contact_is_owner),
                operational_contact_name=operational_contact_name,
                operational_contact_email=operational_contact_email,
                billing_contact_name=billing_contact_name,
                billing_contact_email=billing_contact_email,
                plan_code=plan_code,
            ),
            platform_user_id=user.id,
            base_url=str(request.base_url).rstrip("/"),
        )
        form = {}
        error = None
    except ValueError as exc:
        provisioned = None
        error = str(exc)

    return templates.TemplateResponse("platform.html", {
        "request": request,
        "user": user,
        "active_page": "platform",
        "organizations": _organization_rows(db),
        "plans": platform_plan_definitions(),
        "selected_plan": plan_code or PROFESSIONAL_PLAN_CODE,
        "provisioned": provisioned,
        "form": form,
        "error": error,
    })


@router.get("/invite/{token}")
async def accept_invitation(token: str, request: Request, db: Session = Depends(get_db)):
    invitation = accept_workspace_invitation(db, token=token)
    if not invitation:
        return RedirectResponse(url="/login", status_code=303)

    user = db.query(User).filter(User.email == invitation.email).first()
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    response = RedirectResponse(
        url=post_invitation_acceptance_url(db, invitation),
        status_code=303,
    )
    create_session(response, user.id)
    return response
