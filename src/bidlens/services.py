from sqlalchemy.orm import Session
from sqlalchemy import and_, func, case

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
    archive_reason: str | None = None,
) -> OppState:
    from datetime import datetime as _dt

    opp = db.query(Opportunity).filter(Opportunity.id == opp_id).first()
    if not opp:
        raise ValueError(f"Opportunity {opp_id} not found")

    from_state = OppState(opp.decision_state)
    validate_transition(from_state, to_state)

    opp.decision_state = to_state.value

    if to_state == OppState.ARCHIVED:
        opp.archived_reason = archive_reason
        opp.archived_at = _dt.utcnow()
        opp.archived_by = user_id

    db.commit()

    payload = {"from": from_state.value, "to": to_state.value}
    if archive_reason:
        payload["archive_reason"] = archive_reason

    log_event(
        db,
        event_type="state_changed",
        org_id=org_id,
        user_id=user_id,
        opp_id=opp_id,
        ui_version=ui_version,
        payload=payload,
    )

    return to_state


def cast_vote(
    db: Session,
    *,
    org_id: int,
    user_id: int,
    opp_id: int,
    vote: str,  # "PURSUE" or "PASS"
    ui_version: str = "v1",
) -> dict:
    """Cast or flip a per-user vote on an opportunity.

    Returns dict with vote, state, and whether auto-promotion fired.
    """
    if vote not in ("PURSUE", "PASS"):
        raise ValueError("vote must be PURSUE or PASS")

    opp = db.query(Opportunity).filter(Opportunity.id == opp_id).first()
    if not opp:
        raise ValueError(f"Opportunity {opp_id} not found")

    if opp.decision_state == "ARCHIVED":
        raise ValueError("Cannot vote on archived opportunities")

    # Upsert the vote record
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

    db.flush()

    # Auto-promote: first PURSUE vote moves INBOX -> SHORTLISTED
    promoted = False
    if vote == "PURSUE" and opp.decision_state == "INBOX":
        opp.decision_state = OppState.SHORTLISTED.value
        if not opp.review_stage:
            opp.review_stage = "Team Review"
        promoted = True

    db.commit()

    log_event(
        db,
        event_type="vote_cast",
        org_id=org_id,
        user_id=user_id,
        opp_id=opp_id,
        ui_version=ui_version,
        payload={"vote": vote, "promoted": promoted},
    )

    if promoted:
        log_event(
            db,
            event_type="state_changed",
            org_id=org_id,
            user_id=user_id,
            opp_id=opp_id,
            ui_version=ui_version,
            payload={"from": "INBOX", "to": "SHORTLISTED", "trigger": "auto_promote"},
        )

    return {
        "vote": vote,
        "state": opp.decision_state,
        "promoted": promoted,
    }


def get_vote_counts(db: Session, opp_ids: list[int]) -> dict[int, dict]:
    """Get pursue/pass counts for a list of opportunity IDs.

    Returns {opp_id: {"pursue": N, "pass": N}}.
    """
    if not opp_ids:
        return {}

    rows = (
        db.query(
            Vote.opp_id,
            func.sum(case((Vote.vote == "PURSUE", 1), else_=0)).label("pursue"),
            func.sum(case((Vote.vote == "PASS", 1), else_=0)).label("pass_count"),
        )
        .filter(Vote.opp_id.in_(opp_ids))
        .group_by(Vote.opp_id)
        .all()
    )

    result = {}
    for opp_id, pursue, pass_count in rows:
        result[opp_id] = {"pursue": pursue or 0, "pass": pass_count or 0}

    return result


def get_user_votes(db: Session, user_id: int, opp_ids: list[int]) -> dict[int, str]:
    """Get the current user's vote for a list of opportunity IDs.

    Returns {opp_id: "PURSUE"|"PASS"}.
    """
    if not opp_ids:
        return {}

    rows = (
        db.query(Vote.opp_id, Vote.vote)
        .filter(Vote.user_id == user_id, Vote.opp_id.in_(opp_ids))
        .all()
    )

    return {opp_id: vote for opp_id, vote in rows}


def get_last_activity(db: Session, opp_ids: list[int]) -> dict[int, "datetime"]:
    """Get the most recent vote timestamp for each opportunity.

    Returns {opp_id: datetime}.
    """
    if not opp_ids:
        return {}

    rows = (
        db.query(Vote.opp_id, func.max(Vote.updated_at).label("last_at"))
        .filter(Vote.opp_id.in_(opp_ids))
        .group_by(Vote.opp_id)
        .all()
    )

    return {opp_id: last_at for opp_id, last_at in rows if last_at}
