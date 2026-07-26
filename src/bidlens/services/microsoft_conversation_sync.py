from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from ..models import (
    ExternalIntegrationConnection,
    OpportunityCommunicationMessage,
    OpportunityConversation,
    User,
    Workspace,
)
from .microsoft import (
    PROVIDER_MICROSOFT,
    STATUS_CONNECTED,
    MicrosoftConnectionError,
    MicrosoftConnectionService,
)


TRACKABLE_STATUSES = {"tracked", "tracking_error"}
REAUTHORIZATION_CODES = {"reauthorization_required", "invalid_grant", "permission_missing"}


@dataclass
class ConversationSyncResult:
    conversations_checked: int = 0
    conversations_succeeded: int = 0
    conversations_failed: int = 0
    new_messages_imported: int = 0
    duplicates_skipped: int = 0
    messages_skipped: int = 0
    reauthorization_required: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: Any) -> str | None:
    value = str(value or "").strip()
    return value or None


def _email(record: Any) -> dict[str, str | None]:
    address = record.get("emailAddress") if isinstance(record, dict) else None
    if not isinstance(address, dict):
        return {"address": None, "name": None}
    return {"address": _text(address.get("address")), "name": _text(address.get("name"))}


def _recipients(records: Any) -> list[dict[str, str | None]]:
    return [_email(record) for record in records] if isinstance(records, list) else []


def _timestamp(message: dict[str, Any]) -> datetime | None:
    value = _text(message.get("sentDateTime") or message.get("receivedDateTime"))
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _error_code(exc: Exception) -> str:
    code = getattr(exc, "code", "sync_failed")
    return code if code in {
        "not_connected", "reauthorization_required", "invalid_grant", "permission_missing",
        "provider_throttled", "provider_unavailable", "identity_mismatch",
    } else "sync_failed"


def _refresh_aggregate(db: Session, conversation: OpportunityConversation) -> None:
    rows = (
        db.query(OpportunityCommunicationMessage)
        .filter(
            OpportunityCommunicationMessage.workspace_id == conversation.workspace_id,
            OpportunityCommunicationMessage.conversation_id == conversation.id,
        )
        .all()
    )
    timestamps = [row.provider_timestamp for row in rows if row.provider_timestamp]
    participants: set[str] = set()
    for row in rows:
        if row.sender_address:
            participants.add(row.sender_address.lower())
        for recipient in (row.recipients_json or []) + (row.cc_recipients_json or []):
            address = _text(recipient.get("address")) if isinstance(recipient, dict) else None
            if address:
                participants.add(address.lower())
    conversation.message_count = len(rows)
    conversation.first_message_at = min(timestamps) if timestamps else None
    conversation.last_message_at = max(timestamps) if timestamps else None
    conversation.last_provider_message_at = conversation.last_message_at
    conversation.participant_summary = ", ".join(sorted(participants)) or conversation.participant_summary


def sync_tracked_microsoft_conversations(db: Session, *, workspace: Workspace) -> dict[str, int]:
    """Synchronize only Outlook threads that BidLens previously initiated and tracked."""
    result = ConversationSyncResult()
    conversation_ids = [
        row[0]
        for row in (
            db.query(OpportunityConversation.id)
            .filter(
                OpportunityConversation.workspace_id == workspace.id,
                OpportunityConversation.provider == PROVIDER_MICROSOFT,
                OpportunityConversation.provider_mailbox_id.isnot(None),
                OpportunityConversation.external_conversation_id.isnot(None),
                OpportunityConversation.initial_provider_message_id.isnot(None),
                OpportunityConversation.started_by_user_id.isnot(None),
                OpportunityConversation.tracking_status.in_(TRACKABLE_STATUSES),
            )
            .order_by(OpportunityConversation.id.asc())
            .all()
        )
    ]
    for conversation_id in conversation_ids:
        result.conversations_checked += 1
        try:
            conversation = db.query(OpportunityConversation).filter_by(id=conversation_id).one()
            conversation.last_attempted_sync_at = _now()
            user = db.query(User).filter(User.id == conversation.started_by_user_id).one_or_none()
            connection = (
                db.query(ExternalIntegrationConnection)
                .filter(
                    ExternalIntegrationConnection.workspace_id == workspace.id,
                    ExternalIntegrationConnection.user_id == conversation.started_by_user_id,
                    ExternalIntegrationConnection.provider == PROVIDER_MICROSOFT,
                    ExternalIntegrationConnection.external_user_id == conversation.provider_mailbox_id,
                )
                .one_or_none()
            )
            if not user or not connection or connection.connection_status != STATUS_CONNECTED:
                raise MicrosoftConnectionError("not_connected", "Tracked mailbox is not connected.")
            mailbox_address = _text(connection.connected_email)
            if not mailbox_address:
                raise MicrosoftConnectionError("identity_mismatch", "Connected mailbox identity is unavailable.")
            service = MicrosoftConnectionService(db=db, workspace=workspace, user=user)
            messages = service.list_conversation_messages(conversation.external_conversation_id)
            skipped_for_error = False
            for message in messages:
                provider_id = _text(message.get("id"))
                returned_conversation_id = _text(message.get("conversationId"))
                sender = _email(message.get("sender") or message.get("from"))
                if (
                    message.get("isDraft") is True
                    or not provider_id
                    or returned_conversation_id != conversation.external_conversation_id
                    or not sender["address"]
                ):
                    result.messages_skipped += 1
                    skipped_for_error = True
                    continue
                duplicate = db.query(OpportunityCommunicationMessage.id).filter(
                    OpportunityCommunicationMessage.workspace_id == workspace.id,
                    OpportunityCommunicationMessage.provider == PROVIDER_MICROSOFT,
                    OpportunityCommunicationMessage.provider_mailbox_id == conversation.provider_mailbox_id,
                    OpportunityCommunicationMessage.provider_message_id == provider_id,
                ).first()
                if duplicate:
                    result.duplicates_skipped += 1
                    continue
                body = message.get("body") if isinstance(message.get("body"), dict) else {}
                try:
                    with db.begin_nested():
                        db.add(OpportunityCommunicationMessage(
                            workspace_id=workspace.id,
                            opportunity_id=conversation.opportunity_id,
                            conversation_id=conversation.id,
                            associated_user_id=conversation.started_by_user_id,
                            provider=PROVIDER_MICROSOFT,
                            direction="outbound" if sender["address"].lower() == mailbox_address.lower() else "inbound",
                            provider_mailbox_id=conversation.provider_mailbox_id,
                            provider_message_id=provider_id,
                            provider_conversation_id=returned_conversation_id,
                            internet_message_id=_text(message.get("internetMessageId")),
                            sender_address=sender["address"],
                            sender_display_name=sender["name"],
                            recipients_json=_recipients(message.get("toRecipients")),
                            cc_recipients_json=_recipients(message.get("ccRecipients")),
                            subject=_text(message.get("subject")) or conversation.subject or "(No subject)",
                            body=_text(body.get("content")),
                            body_content_type=_text(body.get("contentType")),
                            provider_timestamp=_timestamp(message),
                            provider_web_link=_text(message.get("webLink")),
                        ))
                        db.flush()
                    result.new_messages_imported += 1
                except IntegrityError:
                    result.duplicates_skipped += 1
            db.flush()
            _refresh_aggregate(db, conversation)
            now = _now()
            conversation.last_successful_sync_at = now
            conversation.tracking_status = "tracking_error" if skipped_for_error else "tracked"
            conversation.last_sync_error = "Some provider messages were invalid and skipped." if skipped_for_error else None
            db.commit()
            result.conversations_succeeded += 1
        except Exception as exc:
            db.rollback()
            code = _error_code(exc)
            failed = db.query(OpportunityConversation).filter_by(id=conversation_id, workspace_id=workspace.id).one_or_none()
            if failed:
                failed.last_attempted_sync_at = _now()
                failed.tracking_status = "tracking_error"
                failed.last_sync_error = code
                db.commit()
            result.conversations_failed += 1
            if code in REAUTHORIZATION_CODES:
                result.reauthorization_required += 1
    return result.to_dict()
