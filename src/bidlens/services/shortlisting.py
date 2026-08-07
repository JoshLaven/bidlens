from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..models import Opportunity, User, Vote


def mark_opportunity_shortlisted_once(
    db: Session,
    opportunity: Opportunity,
    *,
    entered_at: datetime | None = None,
) -> bool:
    """Record the opportunity's first organization-level Shortlist entry once."""
    if opportunity.date_shortlisted is not None:
        return False
    timestamp = entered_at or datetime.now(timezone.utc)
    updated = (
        db.query(Opportunity)
        .filter(
            Opportunity.id == opportunity.id,
            Opportunity.organization_id == opportunity.organization_id,
            Opportunity.date_shortlisted.is_(None),
        )
        .update(
            {Opportunity.date_shortlisted: timestamp},
            synchronize_session="fetch",
        )
    )
    return bool(updated)


def ensure_user_shortlisted(
    db: Session,
    *,
    opportunity: Opportunity,
    user: User,
    now: datetime | None = None,
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
    entered_at = now or datetime.now(timezone.utc)
    mark_opportunity_shortlisted_once(db, opportunity, entered_at=entered_at)
    if row:
        row.vote = "PURSUE"
        row.shortlisted_at = entered_at
    else:
        db.add(
            Vote(
                org_id=opportunity.organization_id,
                opp_id=opportunity.id,
                user_id=user.id,
                vote="PURSUE",
                shortlisted_at=entered_at,
            )
        )
    db.flush()
    return True
