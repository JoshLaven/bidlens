from __future__ import annotations

import re

from sqlalchemy.orm import Session

from ..models import Opportunity, OpportunityPursuitLaneMatch, PursuitLane, PursuitLaneAssignment


def parse_list(value: str | list | None) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = re.split(r"[\n,]+", str(value))
    seen = set()
    items: list[str] = []
    for raw in raw_items:
        item = str(raw).strip()
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            items.append(item)
    return items


def _contains_any(haystack: str | None, needles: list[str], label: str) -> list[str]:
    if not haystack or not needles:
        return []
    haystack_lower = str(haystack).lower()
    return [f"{label} matched {needle}" for needle in needles if needle.lower() in haystack_lower]


def _naics_reasons(opp_naics: str | None, lane_naics: list[str]) -> list[str]:
    if not opp_naics or not lane_naics:
        return []
    opp_values = [part.strip() for part in re.split(r"[\s,;]+", str(opp_naics)) if part.strip()]
    reasons: list[str] = []
    for wanted in lane_naics:
        wanted_lower = wanted.lower()
        if any(value.lower().startswith(wanted_lower) for value in opp_values):
            reasons.append(f"NAICS matched {wanted}")
    return reasons


def lane_match_terms(lane: PursuitLane) -> list[str]:
    """Return the V1 match terms for a lane.

    New lane edits store Match Terms in ``keywords``. Existing lanes may still
    have legacy agencies, NAICS, or set-asides populated, so include those as
    terms until a user edits the lane and the route rewrites it into the V1
    representation.
    """
    terms: list[str] = []
    seen = set()
    for collection in (
        lane.keywords or [],
        lane.agencies or [],
        lane.naics or [],
        lane.set_asides or [],
    ):
        for raw in collection:
            term = str(raw or "").strip()
            key = term.lower()
            if term and key not in seen:
                seen.add(key)
                terms.append(term)
    return terms


def match_lane_to_opportunity(lane: PursuitLane, opportunity: Opportunity) -> list[str]:
    terms = lane_match_terms(lane)
    text = " ".join(
        part
        for part in [
            opportunity.title,
            opportunity.agency,
            opportunity.naics,
            opportunity.naics_title,
            opportunity.set_aside,
            opportunity.description,
            opportunity.description_text,
        ]
        if part
    )
    return _contains_any(text, terms, "Match term")


def refresh_lane_matches(db: Session, organization_id: int, lane: PursuitLane) -> int:
    db.query(OpportunityPursuitLaneMatch).filter(
        OpportunityPursuitLaneMatch.organization_id == organization_id,
        OpportunityPursuitLaneMatch.pursuit_lane_id == lane.id,
    ).delete(synchronize_session=False)

    if not lane.is_active:
        return 0

    opportunities = (
        db.query(Opportunity)
        .filter(Opportunity.organization_id == organization_id)
        .all()
    )
    matched_count = 0
    for opportunity in opportunities:
        reasons = match_lane_to_opportunity(lane, opportunity)
        if not reasons:
            continue
        db.add(
            OpportunityPursuitLaneMatch(
                organization_id=organization_id,
                opportunity_id=opportunity.id,
                pursuit_lane_id=lane.id,
                matched_reasons=reasons,
            )
        )
        matched_count += 1
    return matched_count


def refresh_opportunity_lane_matches(db: Session, organization_id: int, opportunity: Opportunity) -> int:
    db.query(OpportunityPursuitLaneMatch).filter(
        OpportunityPursuitLaneMatch.organization_id == organization_id,
        OpportunityPursuitLaneMatch.opportunity_id == opportunity.id,
    ).delete(synchronize_session=False)

    lanes = (
        db.query(PursuitLane)
        .filter(
            PursuitLane.organization_id == organization_id,
            PursuitLane.is_active.is_(True),
        )
        .all()
    )
    matched_count = 0
    for lane in lanes:
        reasons = match_lane_to_opportunity(lane, opportunity)
        if not reasons:
            continue
        db.add(
            OpportunityPursuitLaneMatch(
                organization_id=organization_id,
                opportunity_id=opportunity.id,
                pursuit_lane_id=lane.id,
                matched_reasons=reasons,
            )
        )
        matched_count += 1
    return matched_count


def refresh_org_lane_matches(db: Session, organization_id: int) -> int:
    db.query(OpportunityPursuitLaneMatch).filter(
        OpportunityPursuitLaneMatch.organization_id == organization_id,
    ).delete(synchronize_session=False)

    lanes = (
        db.query(PursuitLane)
        .filter(
            PursuitLane.organization_id == organization_id,
            PursuitLane.is_active.is_(True),
        )
        .all()
    )
    total = 0
    for lane in lanes:
        total += refresh_lane_matches(db, organization_id, lane)
    return total


def user_my_lanes(db: Session, *, organization_id: int, user_id: int) -> list[PursuitLane]:
    return (
        db.query(PursuitLane)
        .join(PursuitLaneAssignment, PursuitLaneAssignment.pursuit_lane_id == PursuitLane.id)
        .filter(
            PursuitLaneAssignment.organization_id == organization_id,
            PursuitLaneAssignment.user_id == user_id,
            PursuitLane.organization_id == organization_id,
            PursuitLane.is_active.is_(True),
        )
        .order_by(PursuitLane.name.asc())
        .all()
    )


def set_user_my_lanes(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    lane_ids: list[int],
) -> int:
    valid_lane_ids = {
        lane_id
        for (lane_id,) in (
            db.query(PursuitLane.id)
            .filter(
                PursuitLane.organization_id == organization_id,
                PursuitLane.is_active.is_(True),
                PursuitLane.id.in_(lane_ids or [-1]),
            )
            .all()
        )
    }
    db.query(PursuitLaneAssignment).filter(
        PursuitLaneAssignment.organization_id == organization_id,
        PursuitLaneAssignment.user_id == user_id,
    ).delete(synchronize_session=False)

    for lane_id in sorted(valid_lane_ids):
        db.add(
            PursuitLaneAssignment(
                organization_id=organization_id,
                pursuit_lane_id=lane_id,
                user_id=user_id,
            )
        )
    return len(valid_lane_ids)
