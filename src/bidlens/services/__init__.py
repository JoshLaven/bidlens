from sqlalchemy.orm import Session
from sqlalchemy import and_, func, case
from datetime import datetime

from ..models import Opportunity, Vote, User
from ..state_machine import OppState, validate_transition
from ..events import log_event


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

    opp = db.query(Opportunity).filter(
        Opportunity.id == opp_id,
        Opportunity.organization_id == org_id,
    ).first()
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
    toggle_existing: bool = True,
) -> dict:
    """Cast or flip a per-user signal on an opportunity.

    PURSUE is the stored interest signal. It is toggleable and does not move
    an opportunity into CRM or any org-level workflow state.
    """
    if vote not in ("PURSUE", "PASS"):
        raise ValueError("vote must be PURSUE or PASS")

    opp = db.query(Opportunity).filter(
        Opportunity.id == opp_id,
        Opportunity.organization_id == org_id,
    ).first()
    if not opp:
        raise ValueError(f"Opportunity {opp_id} not found")

    if opp.decision_state == "ARCHIVED":
        raise ValueError("Cannot vote on archived opportunities")
    if opp.qualification_status != "qualified":
        raise ValueError("Opportunity must be qualified before users can act on it")

    row = (
        db.query(Vote)
        .filter(and_(Vote.org_id == org_id, Vote.opp_id == opp_id, Vote.user_id == user_id))
        .first()
    )

    toggled_off = False
    if not row:
        row = Vote(org_id=org_id, opp_id=opp_id, user_id=user_id, vote=vote)
        db.add(row)
    elif row.vote == vote and vote in {"PURSUE", "PASS"} and toggle_existing:
        row.vote = None
        toggled_off = True
    else:
        row.vote = vote

    db.flush()

    db.commit()

    effective_vote = None if toggled_off else vote
    log_event(
        db,
        event_type="vote_cast",
        org_id=org_id,
        user_id=user_id,
        opp_id=opp_id,
        ui_version=ui_version,
        payload={"vote": effective_vote, "requested_vote": vote, "toggled_off": toggled_off},
    )

    return {
        "vote": effective_vote,
        "state": opp.decision_state,
        "toggled_off": toggled_off,
    }


def push_opportunity_to_crm(
    db: Session,
    *,
    org_id: int,
    user_id: int,
    opp_id: int,
    ui_version: str = "v1",
) -> Opportunity:
    """Mark an opportunity as locally promoted to CRM.

    This intentionally does not call an external CRM API. CRM remains the
    downstream system of record; BidLens only records that the user promoted it.
    """
    opp = db.query(Opportunity).filter(
        Opportunity.id == opp_id,
        Opportunity.organization_id == org_id,
    ).first()
    if not opp:
        raise ValueError(f"Opportunity {opp_id} not found")
    if opp.decision_state == "ARCHIVED":
        raise ValueError("Cannot push archived opportunities to CRM")
    if opp.qualification_status != "qualified":
        raise ValueError("Opportunity must be qualified before users can act on it")

    interest_row = (
        db.query(Vote)
        .filter(and_(Vote.org_id == org_id, Vote.opp_id == opp_id, Vote.user_id == user_id))
        .first()
    )
    if not interest_row:
        interest_row = Vote(org_id=org_id, opp_id=opp_id, user_id=user_id, vote="PURSUE")
        db.add(interest_row)
    else:
        interest_row.vote = "PURSUE"

    if not opp.crm_pushed:
        opp.crm_pushed = True
        opp.crm_pushed_at = datetime.utcnow()
        opp.crm_pushed_by = user_id
        db.commit()
        log_event(
            db,
            event_type="crm_pushed",
            org_id=org_id,
            user_id=user_id,
            opp_id=opp_id,
            ui_version=ui_version,
            payload={"crm_pushed": True},
        )
    elif opp.crm_pushed_by != user_id:
        # Preserve the original CRM promotion attribution. Other users can
        # signal interest separately after the opportunity is in CRM.
        db.commit()
    else:
        db.commit()
    return opp


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


def get_vote_user_maps(
    db: Session,
    *,
    org_id: int,
    opp_ids: list[int],
) -> tuple[dict[int, list[str]], dict[int, list[str]]]:
    """Get human-readable user lists for pursue/pass votes by opportunity."""
    if not opp_ids:
        return {}, {}

    vote_rows = (
        db.query(Vote.opp_id, Vote.vote, User.name, User.email)
        .join(User, User.id == Vote.user_id)
        .filter(Vote.org_id == org_id, Vote.opp_id.in_(opp_ids))
        .all()
    )

    pursue_users: dict[int, list[str]] = {}
    pass_users: dict[int, list[str]] = {}
    for opp_id, vote, name, email in vote_rows:
        display_name = (name or email or "").strip()
        if not display_name:
            continue
        target = pursue_users if vote == "PURSUE" else pass_users if vote == "PASS" else None
        if target is None:
            continue
        target.setdefault(opp_id, []).append(display_name)

    for value_map in (pursue_users, pass_users):
        for opp_id, users in value_map.items():
            value_map[opp_id] = sorted(dict.fromkeys(users))

    return pursue_users, pass_users


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
