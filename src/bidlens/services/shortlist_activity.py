from __future__ import annotations

import datetime as dt
import re
from typing import Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import (
    Opportunity,
    OpportunityCommunicationMessage,
    OpportunityHistoryEvent,
    OpportunityNote,
    User,
    Workspace,
)
from .opportunity_history import (
    EVENT_GRANTS_FORECAST_VERSION,
    EVENT_GRANTS_SYNOPSIS_VERSION,
    EVENT_SOURCE_UPDATED,
)
from .account_aliases import resolve_account_display_name


OFFICIAL_EVENT_TYPES = (
    EVENT_SOURCE_UPDATED,
    EVENT_GRANTS_SYNOPSIS_VERSION,
    EVENT_GRANTS_FORECAST_VERSION,
)
_PREVIEW_LIMIT = 140


def _compact_text(value: object, *, limit: int = _PREVIEW_LIMIT) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return None
    return text if len(text) <= limit else f"{text[: limit - 1].rstrip()}…"


def _date_label(value: dt.date | dt.datetime | None) -> str | None:
    if value is None:
        return None
    date_value = value.date() if isinstance(value, dt.datetime) else value
    return f"{date_value.strftime('%b')} {date_value.day}, {date_value.year}"


def _official_label(event_type: str, event_data: object) -> str:
    data = event_data if isinstance(event_data, dict) else {}
    if event_type in {EVENT_GRANTS_SYNOPSIS_VERSION, EVENT_GRANTS_FORECAST_VERSION}:
        return _compact_text(data.get("version_name"), limit=80) or "New Version Posted"
    changed = {
        str(field or "").strip().casefold()
        for field in (data.get("changed_fields") or [])
    }
    if "response_deadline" in changed:
        return "Due Date Changed"
    if changed & {"description", "description_text"}:
        return "Description Updated"
    if changed & {"source_stage", "opportunity_type"}:
        return "Opportunity Status Updated"
    return "Opportunity Updated"


def _source_label(source: object) -> str:
    normalized = str(source or "").strip().casefold()
    return {
        "sam": "SAM.gov",
        "sam.gov": "SAM.gov",
        "grants_gov": "Grants.gov",
        "grants.gov": "Grants.gov",
        "govwin_export": "GovWin",
        "govwin_api": "GovWin",
    }.get(normalized, "Official source")


def shortlist_recent_activity(
    db: Session,
    *,
    organization_id: int,
    opportunities: Iterable[Opportunity],
) -> dict[str, dict]:
    """Return one compact official, communication, and note item per visible opportunity."""
    opportunity_list = list(opportunities)
    opportunity_ids = [int(opportunity.id) for opportunity in opportunity_list]
    if not opportunity_ids:
        return {}

    payload = {
        str(opportunity.id): {
            "opportunity": {
                "id": opportunity.id,
                "title": opportunity.title,
                "agency": resolve_account_display_name(opportunity.agency),
                "due_date": _date_label(opportunity.response_deadline),
                "url": f"/opportunity/{opportunity.id}?return_to=shortlist",
            },
            "official_update": None,
            "communication": None,
            "note": None,
        }
        for opportunity in opportunity_list
    }

    official_ranked = (
        db.query(
            OpportunityHistoryEvent.opportunity_id.label("opportunity_id"),
            OpportunityHistoryEvent.event_type.label("event_type"),
            OpportunityHistoryEvent.source.label("source"),
            OpportunityHistoryEvent.occurred_at.label("occurred_at"),
            OpportunityHistoryEvent.event_data.label("event_data"),
            func.row_number().over(
                partition_by=OpportunityHistoryEvent.opportunity_id,
                order_by=(OpportunityHistoryEvent.occurred_at.desc(), OpportunityHistoryEvent.id.desc()),
            ).label("rank"),
        )
        .filter(
            OpportunityHistoryEvent.organization_id == organization_id,
            OpportunityHistoryEvent.opportunity_id.in_(opportunity_ids),
            OpportunityHistoryEvent.event_type.in_(OFFICIAL_EVENT_TYPES),
        )
        .subquery()
    )
    for row in db.query(official_ranked).filter(official_ranked.c.rank == 1).all():
        payload[str(row.opportunity_id)]["official_update"] = {
            "label": _official_label(row.event_type, row.event_data),
            "date": _date_label(row.occurred_at),
            "source": _source_label(row.source),
        }

    workspace_ids = [
        workspace_id
        for (workspace_id,) in db.query(Workspace.id).filter(
            Workspace.organization_id == organization_id
        ).all()
    ]
    if workspace_ids:
        communication_ranked = (
            db.query(
                OpportunityCommunicationMessage.opportunity_id.label("opportunity_id"),
                OpportunityCommunicationMessage.sender_display_name.label("sender_display_name"),
                OpportunityCommunicationMessage.sender_address.label("sender_address"),
                OpportunityCommunicationMessage.subject.label("subject"),
                OpportunityCommunicationMessage.provider_timestamp.label("occurred_at"),
                func.row_number().over(
                    partition_by=OpportunityCommunicationMessage.opportunity_id,
                    order_by=(
                        OpportunityCommunicationMessage.provider_timestamp.desc(),
                        OpportunityCommunicationMessage.id.desc(),
                    ),
                ).label("rank"),
            )
            .filter(
                OpportunityCommunicationMessage.workspace_id.in_(workspace_ids),
                OpportunityCommunicationMessage.opportunity_id.in_(opportunity_ids),
            )
            .subquery()
        )
        for row in db.query(communication_ranked).filter(communication_ranked.c.rank == 1).all():
            payload[str(row.opportunity_id)]["communication"] = {
                "type": "Email",
                "person": _compact_text(row.sender_display_name or row.sender_address, limit=80) or "Unknown sender",
                "date": _date_label(row.occurred_at),
                "preview": _compact_text(row.subject),
            }

    note_ranked = (
        db.query(
            OpportunityNote.opportunity_id.label("opportunity_id"),
            User.name.label("author_name"),
            OpportunityNote.created_at.label("occurred_at"),
            func.substr(OpportunityNote.body, 1, _PREVIEW_LIMIT + 1).label("preview"),
            func.row_number().over(
                partition_by=OpportunityNote.opportunity_id,
                order_by=(OpportunityNote.created_at.desc(), OpportunityNote.id.desc()),
            ).label("rank"),
        )
        .outerjoin(User, User.id == OpportunityNote.user_id)
        .filter(
            OpportunityNote.org_id == organization_id,
            OpportunityNote.opportunity_id.in_(opportunity_ids),
        )
        .subquery()
    )
    for row in db.query(note_ranked).filter(note_ranked.c.rank == 1).all():
        payload[str(row.opportunity_id)]["note"] = {
            "author": _compact_text(row.author_name, limit=80) or "Unknown teammate",
            "date": _date_label(row.occurred_at),
            "preview": _compact_text(row.preview),
        }

    return payload
