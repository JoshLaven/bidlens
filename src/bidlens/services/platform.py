from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import secrets

from sqlalchemy.orm import Session

from ..models import (
    Event,
    Organization,
    OrganizationMembership,
    Plan,
    User,
    Workspace,
    WorkspaceInvitation,
)
from ..tenancy import (
    email_domain,
    ensure_membership,
    normalize_email,
    normalize_org_email_domain,
    slugify_org_name,
    unique_org_slug,
)


PROFESSIONAL_PLAN_CODE = "professional"
PROFESSIONAL_INCLUDED_USERS = 5


@dataclass(frozen=True)
class ProvisionWorkspaceInput:
    organization_name: str
    owner_name: str
    owner_email: str
    operational_contact_is_owner: bool = True
    operational_contact_name: str | None = None
    operational_contact_email: str | None = None
    billing_contact_name: str | None = None
    billing_contact_email: str | None = None
    plan_code: str = PROFESSIONAL_PLAN_CODE


@dataclass(frozen=True)
class ProvisionedWorkspace:
    organization: Organization
    workspace: Workspace
    owner: User
    membership: OrganizationMembership
    plan: Plan
    invitation: WorkspaceInvitation
    invitation_url: str
    email_placeholder: str


def platform_plan_definitions() -> dict[str, dict[str, object]]:
    return {
        PROFESSIONAL_PLAN_CODE: {
            "code": PROFESSIONAL_PLAN_CODE,
            "name": "Professional",
            "included_user_count": PROFESSIONAL_INCLUDED_USERS,
        }
    }


def get_or_create_plan(db: Session, code: str = PROFESSIONAL_PLAN_CODE) -> Plan:
    definitions = platform_plan_definitions()
    definition = definitions.get(code) or definitions[PROFESSIONAL_PLAN_CODE]
    plan = db.query(Plan).filter(Plan.code == definition["code"]).first()
    if not plan:
        plan = Plan(
            code=str(definition["code"]),
            name=str(definition["name"]),
            included_user_count=int(definition["included_user_count"]),
        )
        db.add(plan)
        db.flush()
    else:
        plan.name = str(definition["name"])
        plan.included_user_count = int(definition["included_user_count"])
    return plan


def unique_workspace_slug(db: Session, name: str) -> str:
    base = slugify_org_name(name)
    slug = base
    suffix = 2
    while db.query(Workspace).filter(Workspace.slug == slug).first():
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug


def _required_text(value: str | None, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _optional_text(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def _invitation_token(db: Session) -> str:
    while True:
        token = secrets.token_urlsafe(32)
        if not db.query(WorkspaceInvitation).filter(WorkspaceInvitation.token == token).first():
            return token


def provision_workspace(
    db: Session,
    *,
    payload: ProvisionWorkspaceInput,
    platform_user_id: int | None = None,
    base_url: str = "",
) -> ProvisionedWorkspace:
    organization_name = _required_text(payload.organization_name, "Organization name")
    owner_name = _required_text(payload.owner_name, "Workspace owner name")
    owner_email = normalize_email(_required_text(payload.owner_email, "Workspace owner email"))
    if "@" not in owner_email:
        raise ValueError("Workspace owner email must be a valid email address")

    operational_name = owner_name
    operational_email = owner_email
    billing_name = owner_name
    billing_email = owner_email
    if not payload.operational_contact_is_owner:
        operational_name = _required_text(payload.operational_contact_name, "Operational contact name")
        operational_email = normalize_email(_required_text(payload.operational_contact_email, "Operational contact email"))
        billing_name = _required_text(payload.billing_contact_name, "Billing contact name")
        billing_email = normalize_email(_required_text(payload.billing_contact_email, "Billing contact email"))

    plan = get_or_create_plan(db, payload.plan_code)

    org = Organization(
        name=organization_name,
        slug=unique_org_slug(db, organization_name),
        email_domain=normalize_org_email_domain(email_domain(owner_email)),
        plan=plan.code,
        is_active=True,
        is_live=False,
    )
    db.add(org)
    db.flush()

    workspace = Workspace(
        organization_id=org.id,
        plan_id=plan.id,
        name=f"{organization_name} Workspace",
        slug=unique_workspace_slug(db, organization_name),
        operational_contact_name=operational_name,
        operational_contact_email=operational_email,
        billing_contact_name=billing_name,
        billing_contact_email=billing_email,
    )
    db.add(workspace)
    db.flush()

    owner = db.query(User).filter(User.email == owner_email).first()
    if not owner:
        owner = User(
            email=owner_email,
            name=owner_name,
            organization_id=org.id,
        )
        db.add(owner)
        db.flush()
    else:
        owner.name = owner.name or owner_name
        owner.organization_id = org.id

    membership = ensure_membership(
        db,
        organization_id=org.id,
        user_id=owner.id,
        role="admin",
    )
    membership.role = "admin"

    invitation = WorkspaceInvitation(
        organization_id=org.id,
        workspace_id=workspace.id,
        email=owner_email,
        name=owner_name,
        role="admin",
        token=_invitation_token(db),
        status="pending",
        sent_at=datetime.now(timezone.utc),
    )
    db.add(invitation)

    db.add(Event(
        org_id=org.id,
        user_id=platform_user_id,
        opp_id=None,
        event_type="workspace_provisioned",
        ui_version="platform_v1",
        payload={
            "workspace_id": workspace.id,
            "plan": plan.code,
            "included_user_count": plan.included_user_count,
            "owner_email": owner_email,
        },
    ))
    db.add(Event(
        org_id=org.id,
        user_id=platform_user_id,
        opp_id=None,
        event_type="workspace_invitation_generated",
        ui_version="platform_v1",
        payload={
            "workspace_id": workspace.id,
            "invitation_email": owner_email,
            "email_placeholder": True,
        },
    ))

    db.commit()
    db.refresh(org)
    db.refresh(workspace)
    db.refresh(owner)
    db.refresh(membership)
    db.refresh(plan)
    db.refresh(invitation)

    invite_path = f"/invite/{invitation.token}"
    invitation_url = f"{base_url.rstrip('/')}{invite_path}" if base_url else invite_path
    email_placeholder = (
        f"Invitation email placeholder for {owner_email}: "
        f"Welcome to BidLens. Accept your workspace invitation: {invitation_url}"
    )

    return ProvisionedWorkspace(
        organization=org,
        workspace=workspace,
        owner=owner,
        membership=membership,
        plan=plan,
        invitation=invitation,
        invitation_url=invitation_url,
        email_placeholder=email_placeholder,
    )


def accept_workspace_invitation(db: Session, *, token: str) -> WorkspaceInvitation | None:
    invitation = (
        db.query(WorkspaceInvitation)
        .filter(WorkspaceInvitation.token == token)
        .first()
    )
    if not invitation:
        return None
    if invitation.status != "pending":
        return None

    user = db.query(User).filter(User.email == invitation.email).first()
    if not user:
        user = User(
            email=invitation.email,
            name=invitation.name,
            organization_id=invitation.organization_id,
        )
        db.add(user)
        db.flush()
    else:
        user.name = user.name or invitation.name
        user.organization_id = invitation.organization_id

    membership = ensure_membership(
        db,
        organization_id=invitation.organization_id,
        user_id=user.id,
        role=invitation.role,
    )
    membership.role = invitation.role

    invitation.status = "accepted"
    invitation.accepted_at = datetime.now(timezone.utc)
    db.add(Event(
        org_id=invitation.organization_id,
        user_id=user.id,
        opp_id=None,
        event_type="workspace_invitation_accepted",
        ui_version="platform_v1",
        payload={
            "workspace_id": invitation.workspace_id,
            "invitation_id": invitation.id,
            "invitation_email": invitation.email,
            "development_acceptance": True,
        },
    ))
    db.commit()
    db.refresh(invitation)
    return invitation


def organization_setup_url(organization_id: int) -> str:
    return f"/organization-setup?org_id={organization_id}"


def pre_live_admin_setup_url(
    db: Session,
    user: User,
    *,
    organization_id: int | None = None,
) -> str | None:
    organization_id = organization_id or user.organization_id
    if not organization_id:
        return None

    organization = db.query(Organization).filter(Organization.id == organization_id).first()
    if not organization or organization.is_live:
        return None

    membership = (
        db.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == user.id,
        )
        .first()
    )
    role = (membership.role if membership else "member").strip().lower()
    if role == "admin":
        return organization_setup_url(organization_id)
    return None


def post_setup_completion_url(
    db: Session,
    user: User,
    *,
    organization_id: int | None = None,
    live_url: str,
) -> str:
    setup_url = pre_live_admin_setup_url(db, user, organization_id=organization_id)
    return setup_url or live_url


def post_authentication_destination_url(
    db: Session,
    user: User,
    *,
    organization_id: int | None = None,
) -> str:
    """Return the canonical destination after a workspace user is authenticated."""
    organization_id = organization_id or user.organization_id
    if not organization_id:
        return "/home"

    setup_url = pre_live_admin_setup_url(db, user, organization_id=organization_id)
    if setup_url:
        return setup_url
    return f"/home?org_id={organization_id}"


def post_invitation_acceptance_url(db: Session, invitation: WorkspaceInvitation) -> str:
    """Return the canonical destination after an accepted workspace invitation."""
    user = db.query(User).filter(User.email == invitation.email).first()
    if user:
        return post_authentication_destination_url(
            db,
            user,
            organization_id=invitation.organization_id,
        )

    organization_id = invitation.organization_id
    role = (invitation.role or "member").strip().lower()
    organization = db.query(Organization).filter(Organization.id == organization_id).first()
    if role == "admin" and organization and not organization.is_live:
        return organization_setup_url(organization_id)
    return f"/home?org_id={organization_id}"
