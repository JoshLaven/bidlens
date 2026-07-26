from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..models import (
    Event,
    Opportunity,
    OpportunityConversation,
    OpportunityConversationSendAttempt,
    OrganizationMembership,
    User,
    Vote,
)
from .opportunity_conversations import (
    EVENT_TYPE_CONVERSATION_STARTED,
    create_activity_for_authorized_opportunity,
    create_conversation_for_authorized_opportunity,
    workspace_for_authorized_opportunity,
)


SEND_STATUS_PENDING = "pending"
SEND_STATUS_SENDING = "sending"
SEND_STATUS_ACCEPTED = "accepted_for_delivery"
SEND_STATUS_FAILED = "failed"
SEND_STATUS_UNCERTAIN = "outcome_uncertain"
MAX_RECIPIENTS = 25
MAX_SUBJECT_LENGTH = 180
MAX_MESSAGE_LENGTH = 10000
TOKEN_TTL_MINUTES = 30
EMAIL_RE = re.compile(r"^[A-Z0-9._%+\-']+@[A-Z0-9.\-]+\.[A-Z]{2,}$", re.IGNORECASE)


class OpportunityEmailValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Recipient:
    email: str
    label: str | None = None


def normalize_line_endings(value: str) -> str:
    return (value or "").replace("\r\n", "\n").replace("\r", "\n")


def validate_subject(value: str) -> str:
    subject = " ".join((value or "").split())
    if not subject:
        raise OpportunityEmailValidationError("Subject is required.")
    if len(subject) > MAX_SUBJECT_LENGTH:
        raise OpportunityEmailValidationError(f"Subject must be {MAX_SUBJECT_LENGTH} characters or fewer.")
    return subject


def validate_message(value: str) -> str:
    message = normalize_line_endings(value).strip()
    if not message:
        raise OpportunityEmailValidationError("Message is required.")
    if len(message) > MAX_MESSAGE_LENGTH:
        raise OpportunityEmailValidationError(f"Message must be {MAX_MESSAGE_LENGTH} characters or fewer.")
    return message


def parse_recipient_emails(value: str) -> list[str]:
    raw = re.split(r"[;,]", value or "")
    emails: list[str] = []
    seen: set[str] = set()
    for item in raw:
        email = item.strip()
        if not email:
            continue
        if "\n" in email or "\r" in email:
            raise OpportunityEmailValidationError("Recipient addresses cannot contain line breaks.")
        normalized = email.lower()
        if not EMAIL_RE.match(email):
            raise OpportunityEmailValidationError(f"Invalid recipient email address: {email}")
        if normalized not in seen:
            emails.append(email)
            seen.add(normalized)
    if len(emails) > MAX_RECIPIENTS:
        raise OpportunityEmailValidationError(f"Use {MAX_RECIPIENTS} recipients or fewer.")
    return emails


def interested_colleague_recipients(
    db: Session,
    *,
    opportunity: Opportunity,
    current_user_id: int,
) -> list[Recipient]:
    rows = (
        db.query(User)
        .join(Vote, Vote.user_id == User.id)
        .join(
            OrganizationMembership,
            (OrganizationMembership.user_id == User.id)
            & (OrganizationMembership.organization_id == opportunity.organization_id),
        )
        .filter(
            Vote.org_id == opportunity.organization_id,
            Vote.opp_id == opportunity.id,
            Vote.vote == "PURSUE",
            User.id != current_user_id,
        )
        .order_by(User.name.asc(), User.email.asc())
        .all()
    )
    recipients: list[Recipient] = []
    seen: set[str] = set()
    for user in rows:
        email = (user.email or "").strip()
        if not EMAIL_RE.match(email):
            continue
        normalized = email.lower()
        if normalized in seen:
            continue
        recipients.append(Recipient(email=email, label=user.name or email))
        seen.add(normalized)
    return recipients


def merge_recipients(manual: list[str], colleagues: list[Recipient]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for email in manual + [recipient.email for recipient in colleagues]:
        normalized = email.strip().lower()
        if normalized and normalized not in seen:
            merged.append(email.strip())
            seen.add(normalized)
    if not merged:
        raise OpportunityEmailValidationError("Add at least one recipient.")
    if len(merged) > MAX_RECIPIENTS:
        raise OpportunityEmailValidationError(f"Use {MAX_RECIPIENTS} recipients or fewer.")
    return merged


def ensure_user_shortlisted_from_email(db: Session, *, opportunity: Opportunity, user: User) -> bool:
    """Mark the sender as interested after Microsoft accepts an outbound email.

    Returns True only when the email workflow newly added the opportunity to the
    user's My Shortlist. Existing Pursue votes are left untouched.
    """
    row = (
        db.query(Vote)
        .filter(
            Vote.org_id == opportunity.organization_id,
            Vote.opp_id == opportunity.id,
            Vote.user_id == user.id,
        )
        .first()
    )
    if row and row.vote == "PURSUE":
        return False
    if not row:
        db.add(Vote(org_id=opportunity.organization_id, opp_id=opportunity.id, user_id=user.id, vote="PURSUE"))
    else:
        row.vote = "PURSUE"
    return True


def default_subject(opportunity: Opportunity) -> str:
    return " ".join(f"Discussion: {opportunity.title}".split())[:MAX_SUBJECT_LENGTH]


def body_with_footer(*, message: str, opportunity: Opportunity) -> str:
    footer = f"Sent from BidLens regarding {opportunity.title}"
    return f"{message.strip()}\n\n—\n{footer}"


def participant_summary(recipients: list[str]) -> str:
    if len(recipients) <= 3:
        return ", ".join(recipients)
    return f"{', '.join(recipients[:3])}, +{len(recipients) - 3} more"


def new_send_token() -> str:
    return secrets.token_urlsafe(32)


def send_token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def reserve_send_attempt(
    db: Session,
    *,
    opportunity: Opportunity,
    user: User,
    token: str,
) -> OpportunityConversationSendAttempt:
    workspace = workspace_for_authorized_opportunity(db, opportunity)
    digest = send_token_digest(token)
    existing = (
        db.query(OpportunityConversationSendAttempt)
        .filter(OpportunityConversationSendAttempt.idempotency_key_digest == digest)
        .first()
    )
    if existing:
        if (
            existing.workspace_id != workspace.id
            or existing.opportunity_id != opportunity.id
            or existing.user_id != user.id
        ):
            raise OpportunityEmailValidationError("This compose attempt is not valid for this opportunity.")
        return existing
    attempt = OpportunityConversationSendAttempt(
        workspace_id=workspace.id,
        opportunity_id=opportunity.id,
        user_id=user.id,
        idempotency_key_digest=digest,
        status=SEND_STATUS_PENDING,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=TOKEN_TTL_MINUTES),
    )
    db.add(attempt)
    db.flush()
    return attempt


def validate_send_attempt(attempt: OpportunityConversationSendAttempt, *, opportunity: Opportunity, user: User) -> None:
    now = datetime.now(timezone.utc)
    expires_at = attempt.expires_at if attempt.expires_at.tzinfo else attempt.expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        raise OpportunityEmailValidationError("This compose form expired. Please start again.")
    if attempt.opportunity_id != opportunity.id or attempt.user_id != user.id:
        raise OpportunityEmailValidationError("This compose attempt is not valid for this opportunity.")


def audit_email_send(db: Session, *, opportunity: Opportunity, user: User, outcome: str, error_code: str | None = None) -> None:
    workspace = workspace_for_authorized_opportunity(db, opportunity)
    db.add(Event(
        org_id=opportunity.organization_id,
        user_id=user.id,
        opp_id=opportunity.id,
        event_type="integration_lifecycle",
        payload={
            "provider": "microsoft",
            "workspace_id": workspace.id,
            "outcome": outcome,
            "error_code": error_code,
        },
    ))


def finalize_accepted_send(
    db: Session,
    *,
    opportunity: Opportunity,
    user: User,
    attempt: OpportunityConversationSendAttempt,
    subject: str,
    recipients: list[str],
) -> OpportunityConversation:
    now = datetime.now(timezone.utc)
    conversation = create_conversation_for_authorized_opportunity(
        db,
        opportunity=opportunity,
        provider="microsoft",
        external_conversation_id=None,
        subject=subject,
        started_by_user_id=user.id,
        participant_summary=participant_summary(recipients),
        message_count=1,
        first_message_at=now,
        last_message_at=now,
    )
    conversation.send_status = SEND_STATUS_ACCEPTED
    conversation.send_requested_at = attempt.created_at or now
    conversation.accepted_for_delivery_at = now
    conversation.idempotency_key_digest = attempt.idempotency_key_digest
    conversation.idempotency_expires_at = attempt.expires_at
    conversation.recipient_count = len(recipients)
    db.flush()
    attempt.status = SEND_STATUS_ACCEPTED
    attempt.conversation_id = conversation.id
    attempt.recipient_count = len(recipients)
    create_activity_for_authorized_opportunity(
        db,
        opportunity=opportunity,
        conversation=conversation,
        actor_user_id=user.id,
        event_type=EVENT_TYPE_CONVERSATION_STARTED,
        title=f"{user.name or user.email} started an email conversation.",
        description="Microsoft accepted the email request for sending.",
        metadata_json={"provider": "microsoft", "send_status": SEND_STATUS_ACCEPTED},
        occurred_at=now,
    )
    audit_email_send(db, opportunity=opportunity, user=user, outcome="accepted_for_delivery")
    return conversation
