from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import Opportunity, User, Vote


def ensure_user_shortlisted(
    db: Session,
    *,
    opportunity: Opportunity,
    user: User,
) -> bool:
    """Idempotently add an opportunity to the user's My Shortlist.

    Returns True only when this call changes the user's signal to Interested.
    The caller owns transaction commit and any downstream CRM synchronization.
    """
    row = (
        db.query(Vote)
        .filter(
            Vote.org_id == opportunity.organization_id,
            Vote.opp_id == opportunity.id,
            Vote.user_id == user.id,
        )
        .first()
    )
    if row and row.vote == "PURSUE":
        return False
    if row:
        row.vote = "PURSUE"
    else:
        db.add(
            Vote(
                org_id=opportunity.organization_id,
                opp_id=opportunity.id,
                user_id=user.id,
                vote="PURSUE",
            )
        )
    db.flush()
    return True
