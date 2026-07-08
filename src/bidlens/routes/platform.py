from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..auth import create_session, get_current_user, is_platform_admin_email
from ..database import get_db
from ..models import Organization, Plan, User, Workspace, WorkspaceInvitation
from ..services.platform import (
    PROFESSIONAL_PLAN_CODE,
    accept_workspace_invitation,
    platform_plan_definitions,
    provision_workspace,
    ProvisionWorkspaceInput,
)


router = APIRouter()
templates = Jinja2Templates(directory="src/bidlens/templates")


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
        url=f"/home?org_id={invitation.organization_id}",
        status_code=303,
    )
    create_session(response, user.id)
    return response
