from sqlalchemy.orm import Session
from sqlalchemy import and_

from .models import OpportunityState, Vote
from .state_machine import OppState, validate_transition
from .events import log_event

def get_current_state(db: Session, org_id: int, opp_id: int) -> OppState:
    row = (
        db.query(OpportunityState)
        .filter(and_(OpportunityState.org_id == org_id, OpportunityState.opp_id == opp_id))
        .first()
    )
    if not row:
        return OppState.FEED
    return OppState(row.state)

def transition_state(
    db: Session,
    *,
    org_id: int,
    user_id: int,
    opp_id: int,
    to_state: OppState,
    ui_version: str = "v1",
) -> OppState:
    from_state = get_current_state(db, org_id, opp_id)
    validate_transition(from_state, to_state)

    row = (
        db.query(OpportunityState)
        .filter(and_(OpportunityState.org_id == org_id, OpportunityState.opp_id == opp_id))
        .first()
    )

    if not row:
        row = OpportunityState(
            org_id=org_id,
            opp_id=opp_id,
            state=to_state.value,
            updated_by_user_id=user_id,
        )
        db.add(row)
    else:
        row.state = to_state.value
        row.updated_by_user_id = user_id

    db.commit()

    log_event(
        db,
        event_type="state_changed",
        org_id=org_id,
        user_id=user_id,
        opp_id=opp_id,
        ui_version=ui_version,
        payload={"from": from_state.value, "to": to_state.value},
    )

    return to_state

def set_vote(
    db: Session,
    *,
    org_id: int,
    user_id: int,
    opp_id: int,
    vote: str | None,  # "UP" | "DOWN" | "PASS" | None
    ui_version: str = "v1",
) -> None:
    if vote not in ("UP", "DOWN", "PASS", None):
        raise ValueError("vote must be UP, DOWN, PASS, or null")

    row = (
        db.query(Vote)
        .filter(and_(Vote.org_id == org_id, Vote.opp_id == opp_id, Vote.user_id == user_id))
        .first()
    )

    if not row:
        row = Vote(org_id=org_id, opp_id=opp_id, user_id=user_id, vote=vote)
        db.add(row)
    else:
        row.vote = vote

    db.commit()

    log_event(
        db,
        event_type="vote_cast",
        org_id=org_id,
        user_id=user_id,
        opp_id=opp_id,
        ui_version=ui_version,
        payload={"vote": vote},
    )
