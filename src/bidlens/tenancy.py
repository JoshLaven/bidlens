import re

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from .models import Organization, OrganizationMembership, User

PUBLIC_EMAIL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "outlook.com",
    "hotmail.com",
    "icloud.com",
}


def normalize_email(value: str | None) -> str:
    return str(value or "").strip().lower()


def email_domain(value: str | None) -> str | None:
    email = normalize_email(value)
    if "@" not in email:
        return None
    domain = email.rsplit("@", 1)[-1].strip().lower()
    return domain or None


def is_public_email_domain(domain: str | None) -> bool:
    return (domain or "").strip().lower() in PUBLIC_EMAIL_DOMAINS


def normalize_org_email_domain(value: str | None) -> str | None:
    domain = str(value or "").strip().lower()
    if not domain or is_public_email_domain(domain):
        return None
    return domain


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


def organization_for_email_domain(db: Session, email: str | None) -> Organization | None:
    domain = email_domain(email)
    if not domain or is_public_email_domain(domain):
        return None
    return (
        db.query(Organization)
        .filter(Organization.email_domain == domain)
        .order_by(Organization.id.asc())
        .first()
    )


def ensure_email_domain_membership(db: Session, user: User | None) -> Organization | None:
    if not user or not user.email:
        return None

    matched_org = organization_for_email_domain(db, user.email)
    if not matched_org:
        return None

    ensure_membership(db, organization_id=matched_org.id, user_id=user.id, role="member")
    return matched_org


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

    matched_org = ensure_email_domain_membership(db, user)
    if matched_org:
        return matched_org

    default_org = db.query(Organization).filter(Organization.slug == "default-workspace").first()
    if default_org:
        return default_org

    org = db.query(Organization).order_by(Organization.id.asc()).first()
    if not org:
        raise HTTPException(status_code=500, detail="No organization configured")
    return org


def current_org_id(request: Request, db: Session, user: User | None = None) -> int:
    return current_organization(request, db, user).id
