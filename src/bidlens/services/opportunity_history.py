from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy.orm import Session

from ..models import (
    Opportunity,
    OpportunityHistoryEvent,
    OpportunityHistoryRecipient,
    Vote,
)


EVENT_IMPORTED = "opportunity_imported"
EVENT_SOURCE_UPDATED = "source_updated"
EVENT_SALESFORCE_SYNCHRONIZED = "salesforce_synchronized"
EVENT_GRANTS_SYNOPSIS_VERSION = "grants_synopsis_version"
EVENT_GRANTS_FORECAST_VERSION = "grants_forecast_version"


def record_history_event(
    db: Session,
    *,
    opportunity: Opportunity,
    event_type: str,
    source: str | None = None,
    event_data: dict[str, Any] | None = None,
    occurred_at: dt.datetime | None = None,
    notify_interested: bool = True,
) -> OpportunityHistoryEvent:
    event = OpportunityHistoryEvent(
        organization_id=opportunity.organization_id,
        opportunity_id=opportunity.id,
        event_type=event_type,
        source=source,
        event_data=event_data,
        occurred_at=occurred_at or dt.datetime.utcnow(),
    )
    db.add(event)
    db.flush()

    if notify_interested:
        interested_user_ids = [
            user_id
            for (user_id,) in (
                db.query(Vote.user_id)
                .filter(
                    Vote.org_id == opportunity.organization_id,
                    Vote.opp_id == opportunity.id,
                    Vote.vote == "PURSUE",
                )
                .all()
            )
        ]
        db.add_all(
            OpportunityHistoryRecipient(
                organization_id=opportunity.organization_id,
                opportunity_id=opportunity.id,
                history_event_id=event.id,
                user_id=user_id,
            )
            for user_id in interested_user_ids
        )

    return event


def record_imported_history(
    db: Session,
    opportunity: Opportunity,
) -> OpportunityHistoryEvent:
    return record_history_event(
        db,
        opportunity=opportunity,
        event_type=EVENT_IMPORTED,
        source=opportunity.source,
        event_data={"source_record_id": opportunity.source_record_id},
        occurred_at=opportunity.created_at or opportunity.upserted_at,
        notify_interested=False,
    )


def unread_history_count(
    db: Session,
    *,
    organization_id: int,
    opportunity_id: int,
    user_id: int,
) -> int:
    return (
        db.query(OpportunityHistoryRecipient)
        .join(
            Vote,
            (Vote.org_id == OpportunityHistoryRecipient.organization_id)
            & (Vote.opp_id == OpportunityHistoryRecipient.opportunity_id)
            & (Vote.user_id == OpportunityHistoryRecipient.user_id)
            & (Vote.vote == "PURSUE"),
        )
        .filter(
            OpportunityHistoryRecipient.organization_id == organization_id,
            OpportunityHistoryRecipient.opportunity_id == opportunity_id,
            OpportunityHistoryRecipient.user_id == user_id,
            OpportunityHistoryRecipient.read_at.is_(None),
        )
        .count()
    )


def mark_history_read(
    db: Session,
    *,
    organization_id: int,
    opportunity_id: int,
    user_id: int,
    read_at: dt.datetime | None = None,
) -> int:
    updated = (
        db.query(OpportunityHistoryRecipient)
        .filter(
            OpportunityHistoryRecipient.organization_id == organization_id,
            OpportunityHistoryRecipient.opportunity_id == opportunity_id,
            OpportunityHistoryRecipient.user_id == user_id,
            OpportunityHistoryRecipient.read_at.is_(None),
        )
        .update(
            {OpportunityHistoryRecipient.read_at: read_at or dt.datetime.utcnow()},
            synchronize_session=False,
        )
    )
    db.commit()
    return updated
