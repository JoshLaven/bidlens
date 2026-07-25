from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, joinedload

from ..models import Opportunity, OpportunityActivityEvent, OpportunityConversation, Workspace


EVENT_TYPE_CONVERSATION_STARTED = "conversation_started"
EVENT_TYPE_CONVERSATION_MESSAGE = "conversation_message"
EVENT_TYPE_STATUS_SUMMARY_UPDATED = "status_summary_updated"


@dataclass(frozen=True)
class ConversationContext:
    current_status: dict
    conversations: list[dict]
    recent_activity: list[dict]


DEFAULT_RECENT_ACTIVITY_LIMIT = 10
DEFAULT_CONVERSATION_LIMIT = 25
MAX_TEXT_DISPLAY_LENGTH = 500
DISPLAY_METADATA_KEYS = {"source", "source_label", "status", "conversation_subject"}


class OpportunityConversationTenancyError(ValueError):
    """Raised when conversation/activity data conflicts with opportunity ownership."""


def empty_conversation_context() -> dict:
    return {
        "current_status": {
            "narrative": "No conversation activity has been recorded yet.",
            "last_activity_label": "Not yet available",
            "summary_updated_label": "Automated summary not yet generated.",
            "is_placeholder": True,
        },
        "conversations": [],
        "recent_activity": [],
    }


def _truncate_display_text(value: Any, *, max_length: int = MAX_TEXT_DISPLAY_LENGTH) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "…"


def provider_display_name(provider: str | None) -> str:
    normalized = (provider or "").strip().lower()
    if normalized in {"manual", "seeded", "internal"}:
        return "Internal"
    if normalized == "microsoft_365":
        return "Microsoft 365"
    if normalized == "outlook":
        return "Outlook"
    return "External Provider" if normalized else "Internal"


def actor_display_name(user) -> str:
    if not user:
        return "BidLens"
    return (getattr(user, "name", None) or getattr(user, "email", None) or "BidLens").strip()


def display_safe_metadata(metadata: dict | None) -> dict[str, str]:
    if not isinstance(metadata, dict):
        return {}
    safe = {}
    for key in DISPLAY_METADATA_KEYS:
        value = _truncate_display_text(metadata.get(key), max_length=160)
        if value:
            safe[key] = value
    return safe


def format_activity_timestamp(value: datetime | None) -> str:
    if not value:
        return "Not yet available"
    month = value.strftime("%b")
    hour = value.strftime("%I").lstrip("0") or "0"
    return f"{month} {value.day}, {value.year} {hour}:{value:%M} {value:%p}"


def message_count_label(count: int | None) -> str:
    count = int(count or 0)
    if count == 1:
        return "1 message"
    return f"{count} messages"


def human_activity_text(event: OpportunityActivityEvent) -> str:
    title = _truncate_display_text(event.title)
    if title:
        return title

    actor = actor_display_name(getattr(event, "actor", None))
    event_type = (event.event_type or "").strip()
    if event_type == EVENT_TYPE_CONVERSATION_STARTED:
        return f"{actor} started a conversation."
    if event_type == EVENT_TYPE_CONVERSATION_MESSAGE:
        return f"{actor} added a conversation update."
    if event_type == EVENT_TYPE_STATUS_SUMMARY_UPDATED:
        return "Current status summary refreshed."
    return "Opportunity activity recorded."


def workspace_for_authorized_opportunity(db: Session, opportunity: Opportunity) -> Workspace:
    workspace = (
        db.query(Workspace)
        .filter(Workspace.organization_id == opportunity.organization_id)
        .one_or_none()
    )
    if not workspace:
        raise OpportunityConversationTenancyError(
            "No workspace is associated with the authorized opportunity."
        )
    return workspace


def validate_conversation_for_opportunity(
    conversation: OpportunityConversation,
    *,
    opportunity: Opportunity,
    workspace: Workspace,
) -> None:
    if conversation.workspace_id != workspace.id or conversation.opportunity_id != opportunity.id:
        raise OpportunityConversationTenancyError(
            "Conversation does not belong to the authorized opportunity."
        )


def create_conversation_for_authorized_opportunity(
    db: Session,
    *,
    opportunity: Opportunity,
    subject: str,
    provider: str = "manual",
    external_conversation_id: str | None = None,
    started_by_user_id: int | None = None,
    participant_summary: str | None = None,
    message_count: int = 0,
    first_message_at: datetime | None = None,
    last_message_at: datetime | None = None,
) -> OpportunityConversation:
    workspace = workspace_for_authorized_opportunity(db, opportunity)
    conversation = OpportunityConversation(
        workspace_id=workspace.id,
        opportunity_id=opportunity.id,
        provider=(provider or "manual").strip() or "manual",
        external_conversation_id=external_conversation_id,
        subject=subject,
        started_by_user_id=started_by_user_id,
        participant_summary=participant_summary,
        message_count=max(0, int(message_count or 0)),
        first_message_at=first_message_at,
        last_message_at=last_message_at,
    )
    db.add(conversation)
    return conversation


def create_activity_for_authorized_opportunity(
    db: Session,
    *,
    opportunity: Opportunity,
    event_type: str,
    title: str,
    description: str | None = None,
    actor_user_id: int | None = None,
    conversation: OpportunityConversation | None = None,
    metadata_json: dict | None = None,
    occurred_at: datetime | None = None,
) -> OpportunityActivityEvent:
    workspace = workspace_for_authorized_opportunity(db, opportunity)
    if conversation is not None:
        validate_conversation_for_opportunity(
            conversation,
            opportunity=opportunity,
            workspace=workspace,
        )
    event = OpportunityActivityEvent(
        workspace_id=workspace.id,
        opportunity_id=opportunity.id,
        event_type=event_type,
        actor_user_id=actor_user_id,
        conversation_id=conversation.id if conversation else None,
        title=title,
        description=description,
        metadata_json=metadata_json,
        occurred_at=occurred_at,
    )
    db.add(event)
    return event


def get_opportunity_conversation_context(
    db: Session,
    *,
    opportunity: Opportunity,
    activity_limit: int = DEFAULT_RECENT_ACTIVITY_LIMIT,
    conversation_limit: int = DEFAULT_CONVERSATION_LIMIT,
) -> dict:
    workspace = workspace_for_authorized_opportunity(db, opportunity)
    activity_limit = max(0, min(int(activity_limit), 100))
    conversation_limit = max(0, min(int(conversation_limit), 100))
    conversations = (
        db.query(OpportunityConversation)
        .options(joinedload(OpportunityConversation.started_by_user))
        .filter(
            OpportunityConversation.workspace_id == workspace.id,
            OpportunityConversation.opportunity_id == opportunity.id,
        )
        .order_by(
            OpportunityConversation.last_message_at.desc(),
            OpportunityConversation.id.desc(),
        )
        .limit(conversation_limit)
        .all()
    )
    activity_events = (
        db.query(OpportunityActivityEvent)
        .options(
            joinedload(OpportunityActivityEvent.actor),
            joinedload(OpportunityActivityEvent.conversation),
        )
        .filter(
            OpportunityActivityEvent.workspace_id == workspace.id,
            OpportunityActivityEvent.opportunity_id == opportunity.id,
        )
        .order_by(
            OpportunityActivityEvent.occurred_at.desc(),
            OpportunityActivityEvent.id.desc(),
        )
        .limit(activity_limit)
        .all()
    )

    latest_timestamp = None
    if activity_events:
        latest_timestamp = activity_events[0].occurred_at
    elif conversations:
        latest_timestamp = conversations[0].last_message_at or conversations[0].created_at

    conversation_rows = [
        {
            "id": conversation.id,
            "subject": _truncate_display_text(conversation.subject),
            "provider": conversation.provider,
            "provider_label": provider_display_name(conversation.provider),
            "started_by": actor_display_name(conversation.started_by_user),
            "participants": _truncate_display_text(conversation.participant_summary),
            "message_count_label": message_count_label(conversation.message_count),
            "send_status": conversation.send_status,
            "send_status_label": (
                "Accepted for delivery"
                if conversation.send_status == "accepted_for_delivery"
                else "Outcome uncertain"
                if conversation.send_status == "outcome_uncertain"
                else "Failed"
                if conversation.send_status == "failed"
                else None
            ),
            "last_activity_label": format_activity_timestamp(
                conversation.last_message_at or conversation.created_at
            ),
        }
        for conversation in conversations
    ]
    activity_rows = [
        {
            "id": event.id,
            "event_type": event.event_type,
            "text": human_activity_text(event),
            "description": _truncate_display_text(event.description),
            "metadata": display_safe_metadata(event.metadata_json),
            "actor": actor_display_name(event.actor),
            "conversation_subject": (
                _truncate_display_text(event.conversation.subject)
                if event.conversation
                else None
            ),
            "occurred_at_label": format_activity_timestamp(event.occurred_at),
        }
        for event in activity_events
    ]

    if latest_timestamp:
        status_narrative = (
            "Conversation activity is available for this opportunity. "
            "Automated status summaries will be added in a future phase."
        )
    else:
        status_narrative = "No conversation activity has been recorded yet."

    context = empty_conversation_context()
    context["current_status"]["narrative"] = status_narrative
    context["current_status"]["last_activity_label"] = format_activity_timestamp(latest_timestamp)
    context["conversations"] = conversation_rows
    context["recent_activity"] = activity_rows
    return context
