from __future__ import annotations

import csv
import io
import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth import attach_request_user_context, get_current_user
from ..database import get_db
from ..models import Event, Organization, OrganizationMembership, Plan, User, Workspace, WorkspaceInvitation
from ..services.platform import PROFESSIONAL_PLAN_CODE, get_or_create_plan, unique_workspace_slug
from ..tenancy import (
    current_organization,
    ensure_email_domain_membership,
    ensure_membership,
    normalize_email,
    normalize_org_email_domain,
    unique_org_slug,
)

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="src/bidlens/templates")


class OrganizationIn(BaseModel):
    name: str
    slug: str | None = None
    email_domain: str | None = None


class MembershipIn(BaseModel):
    email: str
    name: str | None = None
    role: str = "member"


class RoleIn(BaseModel):
    role: str


def _role(value: str) -> str:
    role = (value or "member").strip().lower()
    if role not in {"admin", "member"}:
        raise HTTPException(status_code=400, detail="role must be admin or member")
    return role


def _role_label(role: str | None) -> str:
    return "Workspace Admin" if _role(role or "member") == "admin" else "Workspace Member"


def _invitation_token(db: Session) -> str:
    while True:
        token = secrets.token_urlsafe(32)
        if not db.query(WorkspaceInvitation).filter(WorkspaceInvitation.token == token).first():
            return token


def _org_suffix(request: Request, organization_id: int) -> str:
    return "?" + urlencode({"org_id": request.query_params.get("org_id") or str(organization_id)})


def _workspace_for_org(db: Session, org: Organization) -> Workspace:
    workspace = db.query(Workspace).filter(Workspace.organization_id == org.id).first()
    if workspace:
        return workspace

    plan = db.query(Plan).filter(Plan.code == org.plan).first()
    if not plan:
        plan = get_or_create_plan(db, PROFESSIONAL_PLAN_CODE)
    workspace = Workspace(
        organization_id=org.id,
        plan_id=plan.id if plan else None,
        name=f"{org.name} Workspace",
        slug=unique_workspace_slug(db, org.name),
    )
    db.add(workspace)
    db.flush()
    return workspace


def _current_org_or_404(request: Request, db: Session, organization_id: int) -> tuple[User, Organization]:
    user, org = _require_admin(request, db)
    if org.id != organization_id:
        raise HTTPException(status_code=404, detail="Organization not found")
    attach_request_user_context(request, db, user)
    return user, org


def _invitation_url(request: Request, token: str) -> str:
    return f"{str(request.base_url).rstrip('/')}/invite/{token}"


def _create_workspace_invitation(
    db: Session,
    *,
    org: Organization,
    workspace: Workspace,
    email: str,
    name: str | None,
    role: str,
    created_by_user_id: int | None = None,
) -> WorkspaceInvitation:
    clean_email = normalize_email(email)
    if not clean_email or "@" not in clean_email:
        raise ValueError("A valid email address is required.")
    clean_role = _role(role)
    existing = (
        db.query(WorkspaceInvitation)
        .filter(
            WorkspaceInvitation.organization_id == org.id,
            WorkspaceInvitation.email == clean_email,
            WorkspaceInvitation.status == "pending",
        )
        .order_by(WorkspaceInvitation.created_at.desc(), WorkspaceInvitation.id.desc())
        .first()
    )
    if existing:
        existing.name = (name or "").strip() or existing.name
        existing.role = clean_role
        return existing

    invitation = WorkspaceInvitation(
        organization_id=org.id,
        workspace_id=workspace.id,
        email=clean_email,
        name=(name or "").strip() or None,
        role=clean_role,
        token=_invitation_token(db),
        status="pending",
        sent_at=None,
    )
    db.add(invitation)
    db.add(Event(
        org_id=org.id,
        user_id=created_by_user_id,
        opp_id=None,
        event_type="workspace_invitation_generated",
        ui_version="workspace_members_v1",
        payload={
            "workspace_id": workspace.id,
            "invitation_email": clean_email,
            "role": clean_role,
            "email_placeholder": True,
        },
    ))
    return invitation


def _members_context(
    request: Request,
    db: Session,
    *,
    user: User,
    org: Organization,
    message: str | None = None,
    error: str | None = None,
) -> dict:
    workspace = _workspace_for_org(db, org)
    pending = (
        db.query(WorkspaceInvitation)
        .filter(
            WorkspaceInvitation.organization_id == org.id,
            WorkspaceInvitation.status == "pending",
        )
        .order_by(WorkspaceInvitation.created_at.desc(), WorkspaceInvitation.id.desc())
        .all()
    )
    members = (
        db.query(OrganizationMembership, User)
        .join(User, User.id == OrganizationMembership.user_id)
        .filter(OrganizationMembership.organization_id == org.id)
        .order_by(User.email.asc())
        .all()
    )
    return {
        "request": request,
        "user": user,
        "active_page": "administration",
        "organization": org,
        "workspace": workspace,
        "pending_invitations": [
            {
                "invitation": invitation,
                "role_label": _role_label(invitation.role),
                "invite_url": _invitation_url(request, invitation.token),
            }
            for invitation in pending
        ],
        "members": [
            {
                "membership": membership,
                "member": member,
                "role_label": _role_label(membership.role),
            }
            for membership, member in members
        ],
        "page_message": message,
        "error": error,
    }


def _require_admin(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    org = current_organization(request, db, user)
    membership = (
        db.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == org.id,
            OrganizationMembership.user_id == user.id,
        )
        .first()
    )
    if not membership:
        membership = ensure_membership(db, organization_id=org.id, user_id=user.id, role="admin")
        db.flush()

    if membership.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return user, org


def _serialize_org(org: Organization) -> dict:
    return {
        "id": org.id,
        "name": org.name,
        "slug": org.slug,
        "email_domain": org.email_domain,
        "created_at": org.created_at.isoformat() if org.created_at else None,
    }


@router.get("/organizations")
def list_organizations(db: Session = Depends(get_db)):
    return [_serialize_org(org) for org in db.query(Organization).order_by(Organization.id.asc()).all()]


@router.post("/organizations")
def create_organization(payload: OrganizationIn, request: Request, db: Session = Depends(get_db)):
    _require_admin(request, db)

    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    slug = (payload.slug or "").strip() or unique_org_slug(db, name)
    if db.query(Organization).filter(Organization.slug == slug).first():
        raise HTTPException(status_code=400, detail="slug already exists")

    org = Organization(
        name=name,
        slug=slug,
        email_domain=normalize_org_email_domain(payload.email_domain),
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return _serialize_org(org)


@router.get("/organizations/{organization_id}/users")
def list_organization_users(organization_id: int, request: Request, db: Session = Depends(get_db)):
    user, org = _current_org_or_404(request, db, organization_id)

    rows = (
        db.query(OrganizationMembership, User)
        .join(User, User.id == OrganizationMembership.user_id)
        .filter(OrganizationMembership.organization_id == organization_id)
        .order_by(User.email.asc())
        .all()
    )
    wants_json = "application/json" in (request.headers.get("accept") or "")
    if wants_json:
        return [
            {
                "membership_id": membership.id,
                "user_id": user.id,
                "email": user.email,
                "name": user.name,
                "role": membership.role,
                "created_at": membership.created_at.isoformat() if membership.created_at else None,
            }
            for membership, user in rows
        ]

    return templates.TemplateResponse(
        "workspace_members.html",
        _members_context(request, db, user=user, org=org, message=request.query_params.get("message")),
    )


@router.post("/organizations/{organization_id}/invitations")
def create_organization_invitations(
    organization_id: int,
    request: Request,
    emails: list[str] = Form(default=[]),
    names: list[str] = Form(default=[]),
    roles: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user, org = _current_org_or_404(request, db, organization_id)
    workspace = _workspace_for_org(db, org)
    created = 0
    try:
        for index, email in enumerate(emails):
            if not (email or "").strip():
                continue
            _create_workspace_invitation(
                db,
                org=org,
                workspace=workspace,
                email=email,
                name=names[index] if index < len(names) else None,
                role=roles[index] if index < len(roles) else "member",
                created_by_user_id=user.id,
            )
            created += 1
    except ValueError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "workspace_members.html",
            _members_context(request, db, user=user, org=org, error=str(exc)),
            status_code=422,
        )
    db.commit()
    return RedirectResponse(
        url=f"/admin/organizations/{organization_id}/users{_org_suffix(request, organization_id)}&message={created}%20invitation{'s' if created != 1 else ''}%20created",
        status_code=303,
    )


@router.get("/organizations/{organization_id}/invitations/template.csv")
def invitation_csv_template(organization_id: int, request: Request, db: Session = Depends(get_db)):
    _current_org_or_404(request, db, organization_id)
    csv_text = "email,name,role\njohn@company.com,John Smith,member\njane@company.com,Jane Smith,admin\n"
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="bidlens-invite-template.csv"'},
    )


@router.post("/organizations/{organization_id}/invitations/bulk")
async def bulk_create_organization_invitations(
    organization_id: int,
    request: Request,
    csv_file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user, org = _current_org_or_404(request, db, organization_id)
    workspace = _workspace_for_org(db, org)
    if not (csv_file.filename or "").lower().endswith(".csv"):
        return templates.TemplateResponse(
            "workspace_members.html",
            _members_context(request, db, user=user, org=org, error="Upload a CSV file with email,name,role columns."),
            status_code=422,
        )
    content = (await csv_file.read()).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content))
    required = {"email", "name", "role"}
    if not reader.fieldnames or not required.issubset({field.strip() for field in reader.fieldnames}):
        return templates.TemplateResponse(
            "workspace_members.html",
            _members_context(request, db, user=user, org=org, error="CSV must include email,name,role columns."),
            status_code=422,
        )
    created = 0
    try:
        for row in reader:
            if not (row.get("email") or "").strip():
                continue
            _create_workspace_invitation(
                db,
                org=org,
                workspace=workspace,
                email=row.get("email") or "",
                name=row.get("name"),
                role=row.get("role") or "member",
                created_by_user_id=user.id,
            )
            created += 1
    except ValueError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "workspace_members.html",
            _members_context(request, db, user=user, org=org, error=str(exc)),
            status_code=422,
        )
    db.commit()
    return RedirectResponse(
        url=f"/admin/organizations/{organization_id}/users{_org_suffix(request, organization_id)}&message={created}%20invitation{'s' if created != 1 else ''}%20created",
        status_code=303,
    )


@router.post("/organizations/{organization_id}/invitations/{invitation_id}/delete")
def delete_organization_invitation(
    organization_id: int,
    invitation_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    _current_org_or_404(request, db, organization_id)
    invitation = (
        db.query(WorkspaceInvitation)
        .filter(
            WorkspaceInvitation.id == invitation_id,
            WorkspaceInvitation.organization_id == organization_id,
            WorkspaceInvitation.status == "pending",
        )
        .first()
    )
    if not invitation:
        raise HTTPException(status_code=404, detail="Invitation not found")
    invitation.status = "deleted"
    db.commit()
    return RedirectResponse(
        url=f"/admin/organizations/{organization_id}/users{_org_suffix(request, organization_id)}&message=Invitation%20deleted",
        status_code=303,
    )


@router.post("/organizations/{organization_id}/users")
def add_organization_user(
    organization_id: int,
    payload: MembershipIn,
    request: Request,
    db: Session = Depends(get_db),
):
    _current_org_or_404(request, db, organization_id)

    org = db.query(Organization).filter(Organization.id == organization_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    email = normalize_email(payload.email)
    user = db.query(User).filter(User.email == email).first()
    if not user:
        user = User(
            email=email,
            name=payload.name,
            organization_id=organization_id,
        )
        db.add(user)
        db.flush()
    elif payload.name:
        user.name = payload.name

    membership = ensure_membership(
        db,
        organization_id=organization_id,
        user_id=user.id,
        role=_role(payload.role),
    )
    membership.role = _role(payload.role)
    ensure_email_domain_membership(db, user)
    db.commit()
    db.refresh(membership)
    return {
        "membership_id": membership.id,
        "organization_id": organization_id,
        "user_id": user.id,
        "email": user.email,
        "name": user.name,
        "role": membership.role,
    }


@router.post("/organizations/{organization_id}/users/{user_id}/role")
def set_organization_user_role(
    organization_id: int,
    user_id: int,
    payload: RoleIn,
    request: Request,
    db: Session = Depends(get_db),
):
    _current_org_or_404(request, db, organization_id)

    membership = (
        db.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == user_id,
        )
        .first()
    )
    if not membership:
        raise HTTPException(status_code=404, detail="Membership not found")

    membership.role = _role(payload.role)
    db.commit()
    return {
        "membership_id": membership.id,
        "organization_id": organization_id,
        "user_id": user_id,
        "role": membership.role,
    }
