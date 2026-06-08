import re

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from .models import Organization, OrganizationMembership, User


def slugify_org_name(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return base or "workspace"


def unique_org_slug(db: Session, name: str) -> str:
    base = slugify_org_name(name)
    slug = base
    suffix = 2
    while db.query(Organization).filter(Organization.slug == slug).first():
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug


def ensure_membership(db: Session, *, organization_id: int, user_id: int, role: str = "member") -> OrganizationMembership:
    membership = (
        db.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == user_id,
        )
        .first()
    )
    if membership:
        return membership

    membership = OrganizationMembership(
        organization_id=organization_id,
        user_id=user_id,
        role=role,
    )
    db.add(membership)
    return membership


def current_organization(request: Request, db: Session, user: User | None = None) -> Organization:
    """Temporary no-auth workspace resolver.

    V1 behavior intentionally defaults to the first organization and allows
    ?org_id=123 for local development/testing. Full auth/workspace switching
    should replace this resolver later.
    """
    requested_org_id = request.query_params.get("org_id")
    if requested_org_id:
        try:
            org_id = int(requested_org_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="org_id must be an integer")
        org = db.query(Organization).filter(Organization.id == org_id).first()
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")
        return org

    default_org = db.query(Organization).filter(Organization.slug == "default-workspace").first()
    if default_org:
        return default_org

    org = db.query(Organization).order_by(Organization.id.asc()).first()
    if not org:
        raise HTTPException(status_code=500, detail="No organization configured")
    return org


def current_org_id(request: Request, db: Session, user: User | None = None) -> int:
    return current_organization(request, db, user).id
