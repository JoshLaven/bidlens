from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..models import Opportunity, OrgProfile, UserOpportunity, Vote

QUALIFICATION_QUALIFIED = "qualified"


def _parse_csv(text: str | None) -> list[str]:
    if not text:
        return []
    return [value.strip() for value in text.split(",") if value.strip()]


def apply_org_feed_filters(query, db: Session, *, organization_id: int):
    """Apply workspace Feed Rules to an Opportunity query."""
    profile = db.query(OrgProfile).filter(OrgProfile.org_id == organization_id).first()
    if not profile:
        return query

    include_keywords = _parse_csv(profile.include_keywords)
    if include_keywords:
        query = query.filter(
            or_(*(Opportunity.title.ilike(f"%{keyword}%") for keyword in include_keywords))
        )

    for keyword in _parse_csv(profile.exclude_keywords):
        query = query.filter(~Opportunity.title.ilike(f"%{keyword}%"))

    include_agencies = _parse_csv(profile.include_agencies)
    if include_agencies:
        query = query.filter(
            or_(*(Opportunity.agency.ilike(f"%{agency}%") for agency in include_agencies))
        )

    for agency in _parse_csv(profile.exclude_agencies):
        query = query.filter(~Opportunity.agency.ilike(f"%{agency}%"))

    today = date.today()
    if profile.min_days_out is not None:
        query = query.filter(Opportunity.response_deadline >= today + timedelta(days=profile.min_days_out))
    if profile.max_days_out is not None:
        query = query.filter(Opportunity.response_deadline <= today + timedelta(days=profile.max_days_out))

    return query


def exclude_past_due_opportunities(query, *, show_past_due: str = ""):
    if show_past_due == "1":
        return query
    today = date.today()
    return query.filter(
        or_(
            Opportunity.response_deadline.is_(None),
            Opportunity.response_deadline >= today,
        )
    )


def _exclude_inactive_govwin_stages(query):
    source = func.lower(func.coalesce(Opportunity.source, ""))
    raw_source_stage = func.lower(
        func.trim(
            func.coalesce(
                Opportunity.source_stage,
                Opportunity.opportunity_type,
                "",
            )
        )
    )
    return query.filter(
        ~and_(
            source.in_(("govwin_export", "govwin_api")),
            raw_source_stage == "source selection",
        )
    )


def build_feed_query(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    include_watched: bool = True,
):
    """Build the base Feed query before optional UI filters such as lane/search."""
    if include_watched:
        query = (
            db.query(Opportunity, UserOpportunity.watched.label("watched"))
            .outerjoin(
                UserOpportunity,
                and_(
                    UserOpportunity.opportunity_id == Opportunity.id,
                    UserOpportunity.user_id == user_id,
                    UserOpportunity.organization_id == organization_id,
                ),
            )
        )
    else:
        query = db.query(Opportunity)

    query = (
        query.filter(Opportunity.organization_id == organization_id)
        .filter(Opportunity.decision_state != "ARCHIVED")
        .filter(Opportunity.qualification_status == QUALIFICATION_QUALIFIED)
    )

    interested_opp_ids = select(Vote.opp_id).where(
        Vote.org_id == organization_id,
        Vote.user_id == user_id,
        Vote.vote == "PURSUE",
    )
    query = query.filter(~Opportunity.id.in_(interested_opp_ids))

    archived_opp_ids = select(Vote.opp_id).where(
        Vote.org_id == organization_id,
        Vote.user_id == user_id,
        Vote.vote == "PASS",
    )
    query = query.filter(~Opportunity.id.in_(archived_opp_ids))

    return _exclude_inactive_govwin_stages(query)


def feed_awaiting_review_query(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    include_watched: bool = True,
):
    """Build the default Feed population query used by Feed and Daily Brief."""
    query = build_feed_query(
        db,
        organization_id=organization_id,
        user_id=user_id,
        include_watched=include_watched,
    )
    query = apply_org_feed_filters(query, db, organization_id=organization_id)
    return exclude_past_due_opportunities(query)
