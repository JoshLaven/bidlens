from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import Organization, OrganizationMembership, User
from ..tenancy import (
    current_organization,
    ensure_email_domain_membership,
    ensure_membership,
    normalize_email,
    normalize_org_email_domain,
    unique_org_slug,
)

router = APIRouter(prefix="/admin", tags=["admin"])


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
    _require_admin(request, db)

    rows = (
        db.query(OrganizationMembership, User)
        .join(User, User.id == OrganizationMembership.user_id)
        .filter(OrganizationMembership.organization_id == organization_id)
        .order_by(User.email.asc())
        .all()
    )
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


@router.post("/organizations/{organization_id}/users")
def add_organization_user(
    organization_id: int,
    payload: MembershipIn,
    request: Request,
    db: Session = Depends(get_db),
):
    _require_admin(request, db)

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
    _require_admin(request, db)

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
