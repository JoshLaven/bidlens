"""Shared Opportunity Folder authorization primitives."""

from sqlalchemy.orm import Session

from ..models import Opportunity, Workspace


QUALIFICATION_QUALIFIED = "qualified"


def active_organization_id(user) -> int:
    return getattr(user, "current_organization_id", None) or user.organization_id


def user_is_admin(user) -> bool:
    return getattr(user, "current_role", "member") == "admin"


def authorized_opportunity_for_user(db: Session, *, user, opportunity_id: int) -> Opportunity | None:
    opportunity = db.query(Opportunity).filter(
        Opportunity.id == opportunity_id,
        Opportunity.organization_id == active_organization_id(user),
    ).first()
    if opportunity is None:
        return None
    if opportunity.qualification_status != QUALIFICATION_QUALIFIED and not user_is_admin(user):
        return None
    return opportunity


def workspace_for_organization(db: Session, *, organization_id: int) -> Workspace | None:
    return db.query(Workspace).filter(Workspace.organization_id == organization_id).one_or_none()
