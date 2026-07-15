from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import secrets

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..auth import platform_admin_emails
from ..models import (
    CompanyProfile,
    DailySnapshot,
    DigestLog,
    Event,
    GrantsSourceConfig,
    IngestionRun,
    IngestionRunDetail,
    JobRun,
    Opportunity,
    OpportunityBrief,
    OpportunityHistoryEvent,
    OpportunityHistoryRecipient,
    OpportunityNote,
    OpportunityPursuitLaneMatch,
    OpportunityUpdateEvent,
    OrgProfile,
    Organization,
    OrganizationMembership,
    Plan,
    PursuitLane,
    PursuitLaneAssignment,
    SamSourceConfig,
    User,
    UserOpportunity,
    Vote,
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


@dataclass(frozen=True)
class DeleteOrganizationResult:
    organization_id: int
    organization_name: str
    deleted_users: int
    preserved_users: int
    deleted_opportunities: int
    deleted_invitations: int


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


def invitation_url(base_url: str, invitation: WorkspaceInvitation) -> str:
    return f"{base_url.rstrip('/')}/invite/{invitation.token}"


def is_protected_platform_organization(org: Organization | None) -> bool:
    if not org:
        return False
    return org.slug == "bidlens-platform" or org.plan == "platform"


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
    invite_url = f"{base_url.rstrip('/')}{invite_path}" if base_url else invite_path
    email_placeholder = (
        f"Invitation email placeholder for {owner_email}: "
        f"Welcome to BidLens. Accept your workspace invitation: {invite_url}"
    )

    return ProvisionedWorkspace(
        organization=org,
        workspace=workspace,
        owner=owner,
        membership=membership,
        plan=plan,
        invitation=invitation,
        invitation_url=invite_url,
        email_placeholder=email_placeholder,
    )


def create_replacement_workspace_invitation(
    db: Session,
    *,
    invitation: WorkspaceInvitation,
    platform_user_id: int | None = None,
) -> WorkspaceInvitation:
    """Invalidate a pending/consumed invitation and create one replacement."""

    existing_membership = (
        db.query(OrganizationMembership)
        .join(User, User.id == OrganizationMembership.user_id)
        .filter(
            OrganizationMembership.organization_id == invitation.organization_id,
            User.email == invitation.email,
        )
        .first()
    )
    if existing_membership:
        raise ValueError("This user already has an active workspace membership.")

    if invitation.status == "pending":
        invitation.status = "replaced"

    replacement = WorkspaceInvitation(
        organization_id=invitation.organization_id,
        workspace_id=invitation.workspace_id,
        email=invitation.email,
        name=invitation.name,
        role=invitation.role,
        token=_invitation_token(db),
        status="pending",
        sent_at=datetime.now(timezone.utc),
    )
    db.add(replacement)
    db.add(Event(
        org_id=invitation.organization_id,
        user_id=platform_user_id,
        opp_id=None,
        event_type="workspace_invitation_replaced",
        ui_version="platform_v1",
        payload={
            "workspace_id": invitation.workspace_id,
            "old_invitation_id": invitation.id,
            "new_invitation_email": invitation.email,
            "new_invitation_role": invitation.role,
        },
    ))
    db.commit()
    db.refresh(replacement)
    return replacement


def create_owner_replacement_invitation(
    db: Session,
    *,
    organization_id: int,
    platform_user_id: int | None = None,
) -> WorkspaceInvitation:
    organization = db.get(Organization, organization_id)
    workspace = (
        db.query(Workspace)
        .filter(Workspace.organization_id == organization_id)
        .first()
    )
    if not organization or not workspace:
        raise ValueError("Organization workspace was not found.")
    if is_protected_platform_organization(organization):
        raise ValueError("Protected platform organizations cannot receive customer owner invitations.")

    pending_owner = (
        db.query(WorkspaceInvitation)
        .filter(
            WorkspaceInvitation.organization_id == organization_id,
            WorkspaceInvitation.role == "admin",
            WorkspaceInvitation.status == "pending",
        )
        .order_by(WorkspaceInvitation.created_at.desc(), WorkspaceInvitation.id.desc())
        .first()
    )
    if pending_owner:
        return pending_owner

    owner_invitation = (
        db.query(WorkspaceInvitation)
        .filter(
            WorkspaceInvitation.organization_id == organization_id,
            WorkspaceInvitation.role == "admin",
        )
        .order_by(WorkspaceInvitation.created_at.desc(), WorkspaceInvitation.id.desc())
        .first()
    )
    if owner_invitation:
        return create_replacement_workspace_invitation(
            db,
            invitation=owner_invitation,
            platform_user_id=platform_user_id,
        )

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
    if owner_membership and owner_membership.user:
        raise ValueError("An active workspace admin already exists and no invitation is needed.")

    email = workspace.operational_contact_email
    name = workspace.operational_contact_name
    if not email:
        raise ValueError("No owner email is available for this organization.")

    replacement = WorkspaceInvitation(
        organization_id=organization_id,
        workspace_id=workspace.id,
        email=normalize_email(email),
        name=name,
        role="admin",
        token=_invitation_token(db),
        status="pending",
        sent_at=datetime.now(timezone.utc),
    )
    db.add(replacement)
    db.add(Event(
        org_id=organization_id,
        user_id=platform_user_id,
        opp_id=None,
        event_type="workspace_owner_invitation_generated",
        ui_version="platform_v1",
        payload={
            "workspace_id": workspace.id,
            "invitation_email": replacement.email,
        },
    ))
    db.commit()
    db.refresh(replacement)
    return replacement


def delete_test_organization(
    db: Session,
    *,
    organization_id: int,
    confirmation_name: str,
    platform_admin_user_id: int | None = None,
) -> DeleteOrganizationResult:
    org = db.get(Organization, organization_id)
    if not org:
        raise ValueError("Organization was not found.")
    if is_protected_platform_organization(org):
        raise ValueError("The BidLens Platform organization cannot be deleted.")
    if confirmation_name.strip() != org.name:
        raise ValueError("Confirmation name does not match the organization name.")
    organization_name = org.name

    workspace_ids = {
        int(row[0])
        for row in db.query(Workspace.id).filter(Workspace.organization_id == org.id).all()
    }
    opportunity_ids = {
        int(row[0])
        for row in db.query(Opportunity.id).filter(Opportunity.organization_id == org.id).all()
    }
    invitation_count = (
        db.query(WorkspaceInvitation)
        .filter(WorkspaceInvitation.organization_id == org.id)
        .count()
    )

    member_user_ids = {
        int(row[0])
        for row in db.query(OrganizationMembership.user_id)
        .filter(OrganizationMembership.organization_id == org.id)
        .all()
    }
    member_user_ids.update(
        int(row[0])
        for row in db.query(User.id).filter(User.organization_id == org.id).all()
    )
    protected_emails = platform_admin_emails()
    users_to_delete: set[int] = set()
    users_to_preserve: set[int] = set()
    for user_id in member_user_ids:
        user = db.get(User, user_id)
        if not user or user.id == platform_admin_user_id or normalize_email(user.email) in protected_emails:
            users_to_preserve.add(user_id)
            continue
        other_memberships = (
            db.query(OrganizationMembership)
            .filter(
                OrganizationMembership.user_id == user_id,
                OrganizationMembership.organization_id != org.id,
            )
            .count()
        )
        if other_memberships:
            users_to_preserve.add(user_id)
        else:
            users_to_delete.add(user_id)

    ingestion_run_ids = {
        int(row[0])
        for row in db.query(IngestionRun.id)
        .filter(
            or_(
                IngestionRun.organization_id == org.id,
                IngestionRun.user_id.in_(users_to_delete or {-1}),
            )
        )
        .all()
    }
    history_event_ids = {
        int(row[0])
        for row in db.query(OpportunityHistoryEvent.id)
        .filter(
            or_(
                OpportunityHistoryEvent.organization_id == org.id,
                OpportunityHistoryEvent.opportunity_id.in_(opportunity_ids or {-1}),
            )
        )
        .all()
    }

    try:
        db.query(DailySnapshot).filter(
            or_(
                DailySnapshot.workspace_id.in_(workspace_ids or {-1}),
                DailySnapshot.user_id.in_(users_to_delete or {-1}),
            )
        ).delete(synchronize_session=False)
        db.query(OpportunityHistoryRecipient).filter(
            or_(
                OpportunityHistoryRecipient.organization_id == org.id,
                OpportunityHistoryRecipient.opportunity_id.in_(opportunity_ids or {-1}),
                OpportunityHistoryRecipient.history_event_id.in_(history_event_ids or {-1}),
                OpportunityHistoryRecipient.user_id.in_(users_to_delete or {-1}),
            )
        ).delete(synchronize_session=False)
        db.query(OpportunityHistoryEvent).filter(
            or_(
                OpportunityHistoryEvent.organization_id == org.id,
                OpportunityHistoryEvent.opportunity_id.in_(opportunity_ids or {-1}),
            )
        ).delete(synchronize_session=False)
        db.query(OpportunityUpdateEvent).filter(
            or_(
                OpportunityUpdateEvent.organization_id == org.id,
                OpportunityUpdateEvent.opportunity_id.in_(opportunity_ids or {-1}),
                OpportunityUpdateEvent.ingestion_run_id.in_(ingestion_run_ids or {-1}),
            )
        ).delete(synchronize_session=False)
        db.query(IngestionRunDetail).filter(
            or_(
                IngestionRunDetail.ingestion_run_id.in_(ingestion_run_ids or {-1}),
                IngestionRunDetail.matched_opportunity_id.in_(opportunity_ids or {-1}),
            )
        ).delete(synchronize_session=False)
        db.query(IngestionRun).filter(IngestionRun.id.in_(ingestion_run_ids or {-1})).delete(synchronize_session=False)
        db.query(JobRun).filter(JobRun.organization_id == org.id).delete(synchronize_session=False)
        db.query(OpportunityBrief).filter(
            or_(
                OpportunityBrief.organization_id == org.id,
                OpportunityBrief.opportunity_id.in_(opportunity_ids or {-1}),
            )
        ).delete(synchronize_session=False)
        db.query(OpportunityNote).filter(
            or_(
                OpportunityNote.org_id == org.id,
                OpportunityNote.opportunity_id.in_(opportunity_ids or {-1}),
                OpportunityNote.user_id.in_(users_to_delete or {-1}),
            )
        ).delete(synchronize_session=False)
        db.query(UserOpportunity).filter(
            or_(
                UserOpportunity.organization_id == org.id,
                UserOpportunity.opportunity_id.in_(opportunity_ids or {-1}),
                UserOpportunity.user_id.in_(users_to_delete or {-1}),
            )
        ).delete(synchronize_session=False)
        db.query(Vote).filter(
            or_(
                Vote.org_id == org.id,
                Vote.opp_id.in_(opportunity_ids or {-1}),
                Vote.user_id.in_(users_to_delete or {-1}),
            )
        ).delete(synchronize_session=False)
        db.query(OpportunityPursuitLaneMatch).filter(
            or_(
                OpportunityPursuitLaneMatch.organization_id == org.id,
                OpportunityPursuitLaneMatch.opportunity_id.in_(opportunity_ids or {-1}),
            )
        ).delete(synchronize_session=False)
        db.query(PursuitLaneAssignment).filter(
            or_(
                PursuitLaneAssignment.organization_id == org.id,
                PursuitLaneAssignment.user_id.in_(users_to_delete or {-1}),
            )
        ).delete(synchronize_session=False)
        db.query(PursuitLane).filter(PursuitLane.organization_id == org.id).delete(synchronize_session=False)
        db.query(Opportunity).filter(Opportunity.organization_id == org.id).delete(synchronize_session=False)
        db.query(CompanyProfile).filter(CompanyProfile.org_id == org.id).delete(synchronize_session=False)
        db.query(OrgProfile).filter(OrgProfile.org_id == org.id).delete(synchronize_session=False)
        db.query(SamSourceConfig).filter(SamSourceConfig.organization_id == org.id).delete(synchronize_session=False)
        db.query(GrantsSourceConfig).filter(GrantsSourceConfig.organization_id == org.id).delete(synchronize_session=False)
        db.query(DigestLog).filter(DigestLog.org_id == org.id).delete(synchronize_session=False)
        db.query(Event).filter(
            or_(
                Event.org_id == org.id,
                Event.opp_id.in_(opportunity_ids or {-1}),
                Event.user_id.in_(users_to_delete or {-1}),
            )
        ).delete(synchronize_session=False)
        db.query(WorkspaceInvitation).filter(WorkspaceInvitation.organization_id == org.id).delete(synchronize_session=False)
        db.query(OrganizationMembership).filter(OrganizationMembership.organization_id == org.id).delete(synchronize_session=False)
        db.query(Workspace).filter(Workspace.organization_id == org.id).delete(synchronize_session=False)

        for user_id in users_to_preserve:
            user = db.get(User, user_id)
            if user and user.organization_id == org.id:
                replacement_home_org_id = (
                    db.query(OrganizationMembership.organization_id)
                    .filter(
                        OrganizationMembership.user_id == user_id,
                        OrganizationMembership.organization_id != org.id,
                    )
                    .order_by(OrganizationMembership.organization_id.asc())
                    .scalar()
                )
                if not replacement_home_org_id:
                    replacement_home_org_id = (
                        db.query(Organization.id)
                        .filter(Organization.id != org.id)
                        .order_by(Organization.id.asc())
                        .scalar()
                    )
                if not replacement_home_org_id:
                    raise ValueError("Cannot delete organization while preserving users without another organization.")
                user.organization_id = replacement_home_org_id

        db.query(User).filter(User.id.in_(users_to_delete or {-1})).delete(synchronize_session=False)
        db.delete(org)
        db.commit()
    except Exception:
        db.rollback()
        raise

    return DeleteOrganizationResult(
        organization_id=organization_id,
        organization_name=organization_name,
        deleted_users=len(users_to_delete),
        preserved_users=len(users_to_preserve),
        deleted_opportunities=len(opportunity_ids),
        deleted_invitations=invitation_count,
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
