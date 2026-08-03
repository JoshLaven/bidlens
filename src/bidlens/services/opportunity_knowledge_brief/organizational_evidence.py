"""Deterministic GUTS collectors for attributed organizational knowledge."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from ... import config
from ...models import (
    OpportunityCommunicationMessage,
    Opportunity,
    OpportunityNote,
    OrganizationMembership,
    User,
    Workspace,
)
from ..communication_content import clean_message_body, non_substantive_message_reason
from ..organizational_evidence import normalize_evidence_text
from .contracts import EvidenceAuthor, EvidenceCollectionResult, EvidenceSource
from .attribution import normalize_display_name, normalize_email


MIN_MEANINGFUL_CHARACTERS = 2


class EvidenceCollectorScopeError(ValueError):
    """Raised when a collector is invoked with inconsistent tenancy scope."""


def _bounded(value: str, maximum: int) -> tuple[str, bool]:
    if len(value) <= maximum:
        return value, False
    if maximum == 1:
        return "…", True
    return value[: maximum - 1].rstrip() + "…", True


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _date_label(value: datetime | None) -> str:
    return value.strftime("%b %d, %Y") if value else "date unavailable"


def _selection_priority(count: int, maximum: int) -> list[int]:
    """Earliest, recent-heavy, then evenly spread indices, without randomness."""
    if count <= maximum:
        return list(range(count))
    priority = [0]
    recent_quota = min(count - 1, max(1, maximum // 2))
    priority.extend(range(count - 1, count - recent_quota - 1, -1))
    remaining = maximum - len(priority)
    interior = [index for index in range(1, count - recent_quota) if index not in priority]
    if remaining > 0 and interior:
        for slot in range(remaining):
            position = ((slot + 1) * (len(interior) + 1) // (remaining + 1)) - 1
            index = interior[max(0, min(position, len(interior) - 1))]
            if index not in priority:
                priority.append(index)
        priority.extend(index for index in interior if index not in priority)
    return priority


def _select_sources(
    sources: list[EvidenceSource], *, maximum_count: int, maximum_characters: int,
) -> tuple[list[EvidenceSource], Counter]:
    selected_indices: list[int] = []
    used = 0
    for index in _selection_priority(len(sources), maximum_count):
        if len(selected_indices) >= maximum_count:
            break
        size = sources[index].selected_character_count
        if used + size <= maximum_characters:
            selected_indices.append(index)
            used += size
    selected_set = set(selected_indices)
    reasons = Counter()
    for index in range(len(sources)):
        if index not in selected_set:
            reasons["count_limit" if len(selected_indices) >= maximum_count else "total_character_budget"] += 1
    return [sources[index] for index in sorted(selected_indices)], reasons


def _result(
    *, sources: list[EvidenceSource], selected: list[EvidenceSource], queried_count: int,
    omitted: Counter,
) -> EvidenceCollectionResult:
    latest = max((source.occurred_at for source in selected if source.occurred_at), default=None)
    return EvidenceCollectionResult(
        evidence=tuple(selected),
        available_count=queried_count,
        selected_count=len(selected),
        excluded_count=queried_count - len(selected),
        truncated=bool(queried_count != len(selected) or any(source.was_truncated for source in selected)),
        omitted_reason_counts=dict(sorted((key, count) for key, count in omitted.items() if count)),
        latest_source_at=latest,
        total_selected_characters=sum(source.selected_character_count for source in selected),
    )


class NoteEvidenceCollector:
    def __init__(
        self, db: Session, *, maximum_count: int = config.GUTS_MAX_NOTES,
        maximum_note_characters: int = config.GUTS_MAX_NOTE_CHARS,
        maximum_total_characters: int = config.GUTS_MAX_TOTAL_NOTE_CHARS,
    ):
        if min(maximum_count, maximum_note_characters, maximum_total_characters) <= 0:
            raise ValueError("Note collector limits must be positive.")
        self.db = db
        self.maximum_count = maximum_count
        self.maximum_note_characters = maximum_note_characters
        self.maximum_total_characters = maximum_total_characters

    def collect(self, *, opportunity_id: int, organization_id: int) -> EvidenceCollectionResult:
        rows = self.db.query(OpportunityNote).options(joinedload(OpportunityNote.user)).filter(
            OpportunityNote.org_id == organization_id,
            OpportunityNote.opportunity_id == opportunity_id,
        ).order_by(OpportunityNote.created_at.asc(), OpportunityNote.id.asc()).all()
        omitted = Counter()
        sources: list[EvidenceSource] = []
        seen_content: set[str] = set()
        for row in rows:
            normalized = normalize_evidence_text(row.body)
            if not normalized:
                omitted["blank"] += 1
                continue
            if len(normalized) < MIN_MEANINGFUL_CHARACTERS:
                omitted["trivial"] += 1
                continue
            digest = _hash(normalized)
            if digest in seen_content:
                omitted["duplicate_content"] += 1
                continue
            seen_content.add(digest)
            selected_text, truncated = _bounded(normalized, self.maximum_note_characters)
            user = row.user
            email = normalize_email(user.email) if user else None
            display_name = normalize_display_name(user.name if user else None) or email
            author = (
                EvidenceAuthor(user_id=user.id, display_name=display_name, address=email)
                if user and display_name and email else None
            )
            author_label = display_name or "Internal note"
            sources.append(EvidenceSource(
                source_id=f"opportunity_note:{row.id}",
                source_class="organizational_knowledge",
                source_type="note",
                authority="attributed_claim",
                citation_label=f"{author_label} note, {_date_label(row.created_at)}",
                text=selected_text,
                author=author,
                occurred_at=row.created_at,
                content_hash=_hash(selected_text),
                was_truncated=truncated,
                updated_at_source=row.updated_at,
                internal_model_name="OpportunityNote",
                internal_record_id=row.id,
                selected_character_count=len(selected_text),
                original_character_count=len(normalized),
                provenance={"organization_id": organization_id, "opportunity_id": opportunity_id},
            ))
        selected, selection_omissions = _select_sources(
            sources, maximum_count=self.maximum_count,
            maximum_characters=self.maximum_total_characters,
        )
        omitted.update(selection_omissions)
        return _result(sources=sources, selected=selected, queried_count=len(rows), omitted=omitted)


class CommunicationEvidenceCollector:
    def __init__(
        self, db: Session, *, maximum_count: int = config.GUTS_MAX_MESSAGES,
        maximum_message_characters: int = config.GUTS_MAX_MESSAGE_CHARS,
        maximum_total_characters: int = config.GUTS_MAX_TOTAL_COMMUNICATION_CHARS,
    ):
        if min(maximum_count, maximum_message_characters, maximum_total_characters) <= 0:
            raise ValueError("Communication collector limits must be positive.")
        self.db = db
        self.maximum_count = maximum_count
        self.maximum_message_characters = maximum_message_characters
        self.maximum_total_characters = maximum_total_characters

    def collect(
        self, *, opportunity_id: int, organization_id: int, workspace_id: int,
    ) -> EvidenceCollectionResult:
        opportunity = self.db.get(Opportunity, opportunity_id)
        if opportunity is None or opportunity.organization_id != organization_id:
            raise EvidenceCollectorScopeError("Opportunity is outside the requested organization scope.")
        workspace = self.db.get(Workspace, workspace_id)
        if workspace is None or workspace.organization_id != organization_id:
            raise EvidenceCollectorScopeError("Workspace is outside the requested organization scope.")
        rows = self.db.query(OpportunityCommunicationMessage).options(
            joinedload(OpportunityCommunicationMessage.conversation)
        ).filter(
            OpportunityCommunicationMessage.workspace_id == workspace_id,
            OpportunityCommunicationMessage.opportunity_id == opportunity_id,
        ).order_by(
            func.coalesce(
                OpportunityCommunicationMessage.provider_timestamp,
                OpportunityCommunicationMessage.created_at,
            ).asc(),
            OpportunityCommunicationMessage.id.asc(),
        ).all()
        members = self.db.query(User).join(
            OrganizationMembership, OrganizationMembership.user_id == User.id,
        ).filter(
            OrganizationMembership.organization_id == organization_id,
        ).all()
        users_by_email: dict[str, list[User]] = {}
        for user in members:
            email = normalize_email(user.email)
            if email:
                users_by_email.setdefault(email, []).append(user)
        omitted = Counter()
        sources: list[EvidenceSource] = []
        seen_identifiers: set[tuple] = set()
        seen_content: set[str] = set()
        for row in rows:
            internet_id = normalize_evidence_text(row.internet_message_id).casefold()
            provider_id = normalize_evidence_text(row.provider_message_id).casefold()
            if internet_id:
                identity = ("internet", row.provider, row.provider_mailbox_id, internet_id)
            elif provider_id:
                identity = ("provider", row.provider, row.provider_mailbox_id, provider_id)
            else:
                timestamp = row.provider_timestamp or row.created_at
                fallback_body = normalize_evidence_text(clean_message_body(row.body, row.body_content_type))
                identity = (
                    "fallback", normalize_evidence_text(row.sender_address).casefold(),
                    timestamp.isoformat() if timestamp else "", normalize_evidence_text(row.subject).casefold(),
                    _hash(fallback_body),
                )
            if identity in seen_identifiers:
                omitted["duplicate_message"] += 1
                continue
            seen_identifiers.add(identity)
            cleaned = normalize_evidence_text(clean_message_body(row.body, row.body_content_type))
            subject = normalize_evidence_text(row.subject)
            non_substantive_reason = non_substantive_message_reason(
                cleaned_body=cleaned, subject=subject,
            )
            if non_substantive_reason:
                omitted[non_substantive_reason] += 1
                continue
            body_digest = _hash(cleaned)
            if body_digest in seen_content:
                omitted["duplicate_content"] += 1
                continue
            seen_content.add(body_digest)
            selected_text, truncated = _bounded(cleaned, self.maximum_message_characters)
            occurred_at = row.provider_timestamp or row.created_at
            author_name = normalize_display_name(row.sender_display_name)
            author_address = normalize_email(row.sender_address)
            matched_users = users_by_email.get(author_address, []) if author_address else []
            if len(matched_users) == 1:
                matched = matched_users[0]
                matched_email = normalize_email(matched.email)
                matched_name = normalize_display_name(matched.name) or matched_email
                author = EvidenceAuthor(
                    user_id=matched.id, display_name=matched_name, address=matched_email,
                )
            elif author_name or author_address:
                author = EvidenceAuthor(
                    display_name=author_name, address=author_address,
                )
            else:
                author = None
            author_label = (author.display_name if author else None) or author_address or "Unknown sender"
            conversation = row.conversation
            sources.append(EvidenceSource(
                source_id=f"communication:{row.id}",
                source_class="organizational_knowledge",
                source_type="email",
                authority="attributed_claim",
                citation_label=f"{author_label}, {_date_label(occurred_at)}",
                text=selected_text,
                author=author,
                occurred_at=occurred_at,
                content_hash=_hash(selected_text),
                was_truncated=truncated,
                title=subject or None,
                updated_at_source=row.updated_at,
                internal_model_name="OpportunityCommunicationMessage",
                internal_record_id=row.id,
                selected_character_count=len(selected_text),
                original_character_count=len(cleaned),
                provenance={
                    "organization_id": organization_id,
                    "workspace_id": workspace_id,
                    "opportunity_id": opportunity_id,
                    "provider": row.provider,
                    "conversation_id": row.conversation_id,
                    "conversation_subject": normalize_evidence_text(conversation.subject) if conversation else None,
                    "direction": row.direction,
                },
            ))
        selected, selection_omissions = _select_sources(
            sources, maximum_count=self.maximum_count,
            maximum_characters=self.maximum_total_characters,
        )
        omitted.update(selection_omissions)
        return _result(sources=sources, selected=selected, queried_count=len(rows), omitted=omitted)
