"""Access policy for shared GUTS viewing and shortlist-gated generation."""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ...models import Opportunity, Vote
from ..opportunity_access import (
    active_organization_id,
    authorized_opportunity_for_user,
    workspace_for_organization,
)
from .constants import FailureCategory


class GUTSAccessError(RuntimeError):
    failure_category = FailureCategory.ACCESS_DENIED


class GUTSOpportunityNotFoundError(GUTSAccessError):
    failure_category = FailureCategory.OPPORTUNITY_NOT_FOUND


class GUTSShortlistRequiredError(GUTSAccessError):
    failure_category = FailureCategory.SHORTLIST_REQUIRED


class GUTSWorkspaceScopeError(GUTSAccessError):
    failure_category = FailureCategory.ACCESS_DENIED


@dataclass(frozen=True)
class GUTSAccessContext:
    opportunity: Opportunity
    organization_id: int
    workspace_id: int
    user_id: int
    has_pursue_vote: bool
    may_view: bool
    may_generate: bool


def resolve_guts_access(
    db: Session,
    *,
    user,
    opportunity_id: int,
    expected_workspace_id: int | None = None,
) -> GUTSAccessContext:
    organization_id = active_organization_id(user)
    opportunity = authorized_opportunity_for_user(db, user=user, opportunity_id=opportunity_id)
    if opportunity is None:
        # Deliberately conceal whether a cross-organization record exists.
        raise GUTSOpportunityNotFoundError("Opportunity is unavailable.")
    workspace = workspace_for_organization(db, organization_id=organization_id)
    if workspace is None or (expected_workspace_id is not None and workspace.id != expected_workspace_id):
        raise GUTSWorkspaceScopeError("Opportunity workspace scope is unavailable.")
    has_pursue_vote = db.query(Vote.id).filter(
        Vote.org_id == organization_id,
        Vote.opp_id == opportunity.id,
        Vote.user_id == user.id,
        Vote.vote == "PURSUE",
    ).first() is not None
    return GUTSAccessContext(
        opportunity=opportunity,
        organization_id=organization_id,
        workspace_id=workspace.id,
        user_id=user.id,
        has_pursue_vote=has_pursue_vote,
        may_view=True,
        may_generate=has_pursue_vote,
    )


def require_guts_generation_access(
    db: Session,
    *,
    user,
    opportunity_id: int,
    expected_workspace_id: int | None = None,
) -> GUTSAccessContext:
    context = resolve_guts_access(
        db,
        user=user,
        opportunity_id=opportunity_id,
        expected_workspace_id=expected_workspace_id,
    )
    if not context.has_pursue_vote:
        raise GUTSShortlistRequiredError("Add this opportunity to your shortlist before generating a briefing.")
    return context
