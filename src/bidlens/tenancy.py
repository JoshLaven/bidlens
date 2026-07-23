import re

from fastapi import HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import Organization, OrganizationMembership, User, Workspace, WorkspaceInvitation

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


def organizations_for_email_domain(db: Session, email: str | None) -> list[Organization]:
    domain = email_domain(email)
    if not domain or is_public_email_domain(domain):
        return []
    return (
        db.query(Organization)
        .filter(Organization.email_domain == domain)
        .order_by(Organization.id.asc())
        .all()
    )


def organization_for_email_domain(db: Session, email: str | None) -> Organization | None:
    matches = organizations_for_email_domain(db, email)
    if not matches:
        return None

    workspace_org_ids = {
        organization_id
        for (organization_id,) in (
            db.query(Workspace.organization_id)
            .filter(Workspace.organization_id.in_([org.id for org in matches]))
            .all()
        )
    }
    workspace_matches = [org for org in matches if org.id in workspace_org_ids]
    if len(workspace_matches) == 1:
        return workspace_matches[0]
    if len(workspace_matches) > 1:
        raise HTTPException(
            status_code=409,
            detail="Multiple workspaces match this email domain. Contact the platform owner.",
        )
    raise HTTPException(
        status_code=409,
        detail="This email domain is not linked to a provisioned workspace. Contact the platform owner.",
    )


def duplicate_domain_diagnostics(db: Session) -> list[dict]:
    domains = [
        domain
        for (domain,) in (
            db.query(Organization.email_domain)
            .filter(Organization.email_domain.is_not(None))
            .group_by(Organization.email_domain)
            .having(func.count(Organization.id) > 1)
            .all()
        )
    ]
    if not domains:
        return []

    rows = (
        db.query(Organization, Workspace)
        .outerjoin(Workspace, Workspace.organization_id == Organization.id)
        .filter(Organization.email_domain.in_(domains))
        .order_by(Organization.email_domain.asc(), Organization.id.asc())
        .all()
    )
    grouped: dict[str, list[dict]] = {}
    for org, workspace in rows:
        grouped.setdefault(org.email_domain, []).append({
            "organization_id": org.id,
            "organization_name": org.name,
            "organization_slug": org.slug,
            "workspace_id": workspace.id if workspace else None,
            "workspace_name": workspace.name if workspace else None,
            "workspace_slug": workspace.slug if workspace else None,
            "has_workspace": bool(workspace),
        })
    return [
        {
            "email_domain": domain,
            "organizations": organizations,
            "orphaned_organization_ids": [
                item["organization_id"]
                for item in organizations
                if not item["has_workspace"]
            ],
            "workspace_organization_ids": [
                item["organization_id"]
                for item in organizations
                if item["has_workspace"]
            ],
        }
        for domain, organizations in grouped.items()
    ]


def ensure_email_domain_membership(db: Session, user: User | None) -> Organization | None:
    if not user or not user.email:
        return None

    if explicit_memberships_for_user(db, user_id=user.id):
        return None

    matched_org = organization_for_email_domain(db, user.email)
    if not matched_org:
        return None

    ensure_membership(db, organization_id=matched_org.id, user_id=user.id, role="member")
    return matched_org


def explicit_memberships_for_user(db: Session, *, user_id: int) -> list[OrganizationMembership]:
    return (
        db.query(OrganizationMembership)
        .join(Organization, Organization.id == OrganizationMembership.organization_id)
        .filter(
            OrganizationMembership.user_id == user_id,
            Organization.is_active.is_(True),
        )
        .order_by(
            OrganizationMembership.organization_id.asc(),
            OrganizationMembership.id.asc(),
        )
        .all()
    )


def _valid_membership_for_org(
    db: Session,
    *,
    user_id: int,
    organization_id: int,
) -> OrganizationMembership | None:
    return (
        db.query(OrganizationMembership)
        .join(Organization, Organization.id == OrganizationMembership.organization_id)
        .filter(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == user_id,
            Organization.is_active.is_(True),
        )
        .first()
    )


def _organization_for_membership(db: Session, membership: OrganizationMembership | None) -> Organization | None:
    if not membership:
        return None
    return (
        db.query(Organization)
        .filter(
            Organization.id == membership.organization_id,
            Organization.is_active.is_(True),
        )
        .first()
    )


def _admin_memberships(memberships: list[OrganizationMembership]) -> list[OrganizationMembership]:
    return [
        membership
        for membership in memberships
        if str(membership.role or "").strip().lower() == "admin"
    ]


def _admin_invitation_membership(
    db: Session,
    *,
    user: User,
    memberships: list[OrganizationMembership],
) -> OrganizationMembership | None:
    membership_by_org_id = {membership.organization_id: membership for membership in memberships}
    membership_org_ids = list(membership_by_org_id)
    if not membership_by_org_id:
        return None

    invitation = (
        db.query(WorkspaceInvitation)
        .filter(
            WorkspaceInvitation.email == normalize_email(user.email),
            WorkspaceInvitation.role == "admin",
            WorkspaceInvitation.status.in_(("accepted", "pending")),
            WorkspaceInvitation.organization_id.in_(membership_org_ids),
        )
        .order_by(
            WorkspaceInvitation.created_at.desc(),
            WorkspaceInvitation.id.desc(),
        )
        .first()
    )
    if not invitation:
        return None
    membership = membership_by_org_id.get(invitation.organization_id)
    if membership and str(membership.role or "").strip().lower() == "admin":
        return membership
    return None


def resolve_user_organization(
    db: Session,
    user: User,
    *,
    requested_org_id: int | None = None,
) -> Organization | None:
    """Resolve a user's current workspace without letting domain fallback win over explicit access."""

    memberships = explicit_memberships_for_user(db, user_id=user.id)

    if requested_org_id is not None:
        membership = _valid_membership_for_org(
            db,
            user_id=user.id,
            organization_id=requested_org_id,
        )
        if not membership:
            raise HTTPException(status_code=403, detail="You do not have access to this workspace.")
        return _organization_for_membership(db, membership)

    invitation_membership = _admin_invitation_membership(db, user=user, memberships=memberships)
    invitation_org = _organization_for_membership(db, invitation_membership)
    if invitation_org:
        return invitation_org

    admin_memberships = _admin_memberships(memberships)
    if admin_memberships:
        preferred_admin = next(
            (
                membership
                for membership in admin_memberships
                if membership.organization_id == user.organization_id
            ),
            admin_memberships[0],
        )
        return _organization_for_membership(db, preferred_admin)

    if user.organization_id:
        membership = _valid_membership_for_org(
            db,
            user_id=user.id,
            organization_id=user.organization_id,
        )
        org = _organization_for_membership(db, membership)
        if org:
            return org

    if memberships:
        return _organization_for_membership(db, memberships[0])

    return ensure_email_domain_membership(db, user)


def current_organization(request: Request, db: Session, user: User | None = None) -> Organization:
    """Temporary no-auth workspace resolver.

    V1 behavior intentionally defaults to the first organization and allows
    ?org_id=123 for local development/testing. Full auth/workspace switching
    should replace this resolver later.
    """
    requested_org_id = request.query_params.get("org_id")
    parsed_requested_org_id = None
    if requested_org_id:
        try:
            parsed_requested_org_id = int(requested_org_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="org_id must be an integer")

    if user:
        resolved_org = resolve_user_organization(
            db,
            user,
            requested_org_id=parsed_requested_org_id,
        )
        if resolved_org:
            return resolved_org
        if parsed_requested_org_id is not None:
            raise HTTPException(status_code=404, detail="Organization not found")

    if parsed_requested_org_id is not None:
        org = db.query(Organization).filter(Organization.id == parsed_requested_org_id).first()
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
