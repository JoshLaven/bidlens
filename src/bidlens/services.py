from sqlalchemy.orm import Session
from sqlalchemy import and_

from .models import Opportunity, Vote
from .state_machine import OppState, validate_transition
from .events import log_event


def transition_state(
    db: Session,
    *,
    org_id: int,
    user_id: int,
    opp_id: int,
    to_state: OppState,
    ui_version: str = "v1",
) -> OppState:
    opp = db.query(Opportunity).filter(Opportunity.id == opp_id).first()
    if not opp:
        raise ValueError(f"Opportunity {opp_id} not found")

    from_state = OppState(opp.decision_state)
    validate_transition(from_state, to_state)

    opp.decision_state = to_state.value
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
