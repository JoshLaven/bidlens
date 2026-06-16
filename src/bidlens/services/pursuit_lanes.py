from __future__ import annotations

import re

from sqlalchemy.orm import Session

from ..models import Opportunity, OpportunityPursuitLaneMatch, PursuitLane


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


def match_lane_to_opportunity(lane: PursuitLane, opportunity: Opportunity) -> list[str]:
    reasons: list[str] = []
    reasons.extend(_contains_any(opportunity.agency, lane.agencies or [], "Agency"))
    reasons.extend(_naics_reasons(opportunity.naics, lane.naics or []))
    reasons.extend(_contains_any(opportunity.set_aside, lane.set_asides or [], "Set-aside"))

    text = " ".join(
        part
        for part in [
            opportunity.title,
            opportunity.description,
            opportunity.description_text,
        ]
        if part
    )
    reasons.extend(_contains_any(text, lane.keywords or [], "Keyword"))
    return reasons


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
