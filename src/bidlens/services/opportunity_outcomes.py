from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..events import log_event
from ..models import Opportunity, OpportunityOutcome, Vote

OUTCOME_BIDDING = "bidding"
OUTCOME_NO_BID = "no_bid"
VALID_OUTCOMES = {OUTCOME_BIDDING, OUTCOME_NO_BID}
QUALIFICATION_QUALIFIED = "qualified"


OutcomeType = Literal["bidding", "no_bid"]


def _pursued_before_deadline_exists(*, organization_id: int):
    return (
        select(Vote.id)
        .where(
            Vote.org_id == organization_id,
            Vote.opp_id == Opportunity.id,
            Vote.vote == "PURSUE",
            func.date(Vote.updated_at) <= Opportunity.response_deadline,
        )
        .correlate(Opportunity)
        .exists()
    )


def _existing_outcome_exists(*, organization_id: int):
    return (
        select(OpportunityOutcome.id)
        .where(
            OpportunityOutcome.organization_id == organization_id,
            OpportunityOutcome.opportunity_id == Opportunity.id,
        )
        .correlate(Opportunity)
        .exists()
    )


def past_due_outcome_workflow_visible_exists(*, organization_id: int):
    """Return a correlated predicate for opportunities accessible via Past Due.

    My Shortlist uses this to hide only overdue items that have a real Past Due
    destination: either pending outcome review or a previously recorded outcome.
    Overdue Interested items that fail the strict Past Due eligibility rule stay
    visible in My Shortlist rather than falling into limbo.
    """
    today = date.today()
    return and_(
        Opportunity.response_deadline.is_not(None),
        Opportunity.response_deadline < today,
        or_(
            _pursued_before_deadline_exists(organization_id=organization_id),
            _existing_outcome_exists(organization_id=organization_id),
        ),
    )


def unresolved_past_due_outcome_query(db: Session, *, organization_id: int):
    """Qualified, previously shortlisted opportunities needing workspace outcome review.

    This intentionally does not use Vote.PASS or Opportunity.decision_state=ARCHIVED:
    past-due bid outcomes are official workspace decisions, while PASS remains a
    personal archive signal.
    """
    pursued_before_deadline = _pursued_before_deadline_exists(organization_id=organization_id)
    existing_outcome = _existing_outcome_exists(organization_id=organization_id)
    today = date.today()
    return (
        db.query(Opportunity)
        .filter(Opportunity.organization_id == organization_id)
        .filter(Opportunity.decision_state != "ARCHIVED")
        .filter(Opportunity.qualification_status == QUALIFICATION_QUALIFIED)
        .filter(Opportunity.response_deadline.is_not(None))
        .filter(Opportunity.response_deadline < today)
        .filter(pursued_before_deadline)
        .filter(~existing_outcome)
    )


def unresolved_past_due_outcome_count(db: Session, *, organization_id: int) -> int:
    return unresolved_past_due_outcome_query(db, organization_id=organization_id).count()


def unresolved_past_due_outcomes(db: Session, *, organization_id: int) -> list[Opportunity]:
    return (
        unresolved_past_due_outcome_query(db, organization_id=organization_id)
        .order_by(Opportunity.response_deadline.asc(), Opportunity.id.asc())
        .all()
    )


def record_opportunity_outcome(
    db: Session,
    *,
    organization_id: int,
    opportunity_id: int,
    outcome_type: str,
    recorded_by: int,
    ui_version: str = "v1",
) -> OpportunityOutcome:
    if outcome_type not in VALID_OUTCOMES:
        raise ValueError("Invalid outcome type")

    opportunity = (
        db.query(Opportunity)
        .filter(
            Opportunity.id == opportunity_id,
            Opportunity.organization_id == organization_id,
            Opportunity.qualification_status == QUALIFICATION_QUALIFIED,
        )
        .first()
    )
    if not opportunity:
        raise ValueError("Opportunity not found")

    now = datetime.now(timezone.utc)
    outcome = (
        db.query(OpportunityOutcome)
        .filter(
            OpportunityOutcome.organization_id == organization_id,
            OpportunityOutcome.opportunity_id == opportunity_id,
        )
        .first()
    )
    if outcome:
        outcome.outcome_type = outcome_type
        outcome.recorded_by = recorded_by
        outcome.recorded_at = now
    else:
        eligible = (
            unresolved_past_due_outcome_query(db, organization_id=organization_id)
            .filter(Opportunity.id == opportunity_id)
            .first()
        )
        if not eligible:
            raise ValueError("Opportunity is not eligible for past-due outcome review")
        outcome = OpportunityOutcome(
            organization_id=organization_id,
            opportunity_id=opportunity_id,
            outcome_type=outcome_type,
            recorded_by=recorded_by,
            recorded_at=now,
        )
        db.add(outcome)

    db.flush()
    log_event(
        db,
        event_type="opportunity_outcome_recorded",
        org_id=organization_id,
        user_id=recorded_by,
        opp_id=opportunity_id,
        ui_version=ui_version,
        payload={
            "outcome_type": outcome_type,
            "message": "Marked as We’re Bidding"
            if outcome_type == OUTCOME_BIDDING
            else "Marked as No Bid",
        },
    )
    db.refresh(outcome)
    return outcome
