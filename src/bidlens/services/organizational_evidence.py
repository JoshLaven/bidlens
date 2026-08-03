"""Shared stored communication and note evidence collection for BidLens AI features."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import re
import unicodedata

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from ..models import (
    Opportunity, OpportunityCommunicationMessage, OpportunityNote,
    OrganizationMembership, User, Workspace,
)
from .communication_content import clean_message_body, non_substantive_message_reason
from .identity_normalization import normalize_display_name, normalize_email
from .organizational_evidence_contracts import (
    OrganizationalEvidenceAuthor, OrganizationalEvidenceCollection,
    OrganizationalEvidenceItem, OrganizationalEvidenceSelectionPolicy,
    TeamSummaryEvidenceBundle, fingerprint_team_summary_evidence,
)


MIN_MEANINGFUL_CHARACTERS = 2


class OrganizationalEvidenceScopeError(ValueError):
    """Raised when organizational evidence is requested outside its tenant scope."""


def normalize_evidence_text(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    normalized = "".join(
        character for character in normalized
        if character in "\n\t" or unicodedata.category(character) != "Cc"
    )
    lines = [
        re.sub(r"[ \t]+", " ", line).strip()
        for line in normalized.replace("\r", "\n").split("\n")
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bounded(value: str, maximum: int) -> tuple[str, bool]:
    if len(value) <= maximum:
        return value, False
    if maximum == 1:
        return "…", True
    return value[: maximum - 1].rstrip() + "…", True


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


def _select_items(
    items: list[OrganizationalEvidenceItem], policy: OrganizationalEvidenceSelectionPolicy,
) -> tuple[list[OrganizationalEvidenceItem], Counter]:
    selected_indices: list[int] = []
    used = 0
    for index in _selection_priority(len(items), policy.maximum_count):
        if len(selected_indices) >= policy.maximum_count:
            break
        size = len(items[index].text)
        if used + size <= policy.maximum_total_characters:
            selected_indices.append(index)
            used += size
    selected_set = set(selected_indices)
    reasons = Counter()
    for index in range(len(items)):
        if index not in selected_set:
            reasons[
                "count_limit" if len(selected_indices) >= policy.maximum_count
                else "total_character_budget"
            ] += 1
    return [items[index] for index in sorted(selected_indices)], reasons


def _author_key(author: OrganizationalEvidenceAuthor) -> str:
    return author.identity_fingerprint


def _time_key(value: datetime | None) -> float:
    if value is None:
        return float("-inf")
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return normalized.timestamp()


def _external_or_authorless(name: str | None, address: str | None) -> OrganizationalEvidenceAuthor:
    if name or address:
        return OrganizationalEvidenceAuthor(
            kind="external_person", display_name=name, address=address,
        )
    return OrganizationalEvidenceAuthor(kind="authorless")


class StoredCommunicationEvidenceCollector:
    def __init__(
        self, db: Session, *, policy: OrganizationalEvidenceSelectionPolicy,
        exclude_non_substantive: bool = True,
        preserve_separately_authored_content: bool = True,
    ):
        self.db = db
        self.policy = policy
        self.exclude_non_substantive = exclude_non_substantive
        self.preserve_separately_authored_content = preserve_separately_authored_content

    def collect(
        self, *, opportunity_id: int, organization_id: int, workspace_id: int,
    ) -> OrganizationalEvidenceCollection:
        opportunity = self.db.get(Opportunity, opportunity_id)
        workspace = self.db.get(Workspace, workspace_id)
        if opportunity is None or opportunity.organization_id != organization_id:
            raise OrganizationalEvidenceScopeError("Opportunity is outside the requested organization scope.")
        if workspace is None or workspace.organization_id != organization_id:
            raise OrganizationalEvidenceScopeError("Workspace is outside the requested organization scope.")
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
        ).filter(OrganizationMembership.organization_id == organization_id).all()
        users_by_email: dict[str, list[User]] = {}
        for user in members:
            address = normalize_email(user.email)
            if address:
                users_by_email.setdefault(address, []).append(user)

        omitted = Counter()
        items: list[OrganizationalEvidenceItem] = []
        seen_identifiers: set[tuple] = set()
        seen_content: set[tuple[str, str] | str] = set()
        for row in rows:
            internet_id = normalize_evidence_text(row.internet_message_id).casefold()
            provider_id = normalize_evidence_text(row.provider_message_id).casefold()
            fallback_body = normalize_evidence_text(clean_message_body(row.body, row.body_content_type))
            if internet_id:
                identity = ("internet", row.provider, row.provider_mailbox_id, internet_id)
            elif provider_id:
                identity = ("provider", row.provider, row.provider_mailbox_id, provider_id)
            else:
                timestamp = row.provider_timestamp or row.created_at
                identity = (
                    "fallback", normalize_evidence_text(row.sender_address).casefold(),
                    timestamp.isoformat() if timestamp else "",
                    normalize_evidence_text(row.subject).casefold(), _hash(fallback_body),
                )
            if identity in seen_identifiers:
                omitted["duplicate_message"] += 1
                continue
            seen_identifiers.add(identity)
            cleaned = fallback_body
            subject = normalize_evidence_text(row.subject)
            reason = non_substantive_message_reason(cleaned_body=cleaned, subject=subject)
            if self.exclude_non_substantive and reason:
                omitted[reason] += 1
                continue
            if not cleaned:
                omitted["empty_original_content"] += 1
                continue

            author_name = normalize_display_name(row.sender_display_name)
            author_address = normalize_email(row.sender_address)
            matched_users = users_by_email.get(author_address, []) if author_address else []
            if len(matched_users) == 1:
                matched = matched_users[0]
                matched_address = normalize_email(matched.email)
                matched_name = normalize_display_name(matched.name) or matched_address
                author = OrganizationalEvidenceAuthor(
                    kind="internal_user", user_id=matched.id,
                    display_name=matched_name, address=matched_address,
                )
            else:
                author = _external_or_authorless(author_name, author_address)
            body_digest = _hash(cleaned)
            content_key: tuple[str, str] | str = (
                (body_digest, _author_key(author))
                if self.preserve_separately_authored_content else body_digest
            )
            if content_key in seen_content:
                omitted["duplicate_content"] += 1
                continue
            seen_content.add(content_key)
            selected_text, truncated = _bounded(cleaned, self.policy.maximum_item_characters)
            occurred_at = row.provider_timestamp or row.created_at
            recipients = tuple(
                normalize_email(item.get("address"))
                for item in (row.recipients_json or []) if isinstance(item, dict)
                and normalize_email(item.get("address"))
            )
            items.append(OrganizationalEvidenceItem(
                source_id=f"communication:{row.id}", source_type="communication",
                occurred_at=occurred_at, updated_at=row.updated_at, text=selected_text,
                content_hash=_hash(selected_text), author=author, title=subject or None,
                direction=row.direction if row.direction in {"inbound", "outbound"} else None,
                recipients=recipients,
                stable_identity={
                    "provider": row.provider,
                    "internal_record_id": row.id,
                    "conversation_id": row.conversation_id,
                },
                was_truncated=truncated, original_character_count=len(cleaned),
            ))
        selected, selection_omissions = _select_items(items, self.policy)
        omitted.update(selection_omissions)
        return OrganizationalEvidenceCollection(
            items=tuple(selected), available_count=len(rows), selected_count=len(selected),
            omitted_reason_counts=dict(sorted(omitted.items())),
            total_selected_characters=sum(len(item.text) for item in selected),
            truncated=bool(len(rows) != len(selected) or any(item.was_truncated for item in selected)),
        )


class StoredNoteEvidenceCollector:
    def __init__(
        self, db: Session, *, policy: OrganizationalEvidenceSelectionPolicy,
        preserve_separately_authored_content: bool = True,
    ):
        self.db = db
        self.policy = policy
        self.preserve_separately_authored_content = preserve_separately_authored_content

    def collect(
        self, *, opportunity_id: int, organization_id: int, workspace_id: int,
    ) -> OrganizationalEvidenceCollection:
        opportunity = self.db.get(Opportunity, opportunity_id)
        workspace = self.db.get(Workspace, workspace_id)
        if opportunity is None or opportunity.organization_id != organization_id:
            raise OrganizationalEvidenceScopeError("Opportunity is outside the requested organization scope.")
        if workspace is None or workspace.organization_id != organization_id:
            raise OrganizationalEvidenceScopeError("Workspace is outside the requested organization scope.")
        rows = self.db.query(OpportunityNote).options(joinedload(OpportunityNote.user)).filter(
            OpportunityNote.org_id == organization_id,
            OpportunityNote.opportunity_id == opportunity_id,
        ).order_by(OpportunityNote.created_at.asc(), OpportunityNote.id.asc()).all()
        omitted = Counter()
        items: list[OrganizationalEvidenceItem] = []
        seen_content: set[tuple[str, str] | str] = set()
        for row in rows:
            normalized = normalize_evidence_text(row.body)
            if not normalized:
                omitted["blank"] += 1
                continue
            if len(normalized) < MIN_MEANINGFUL_CHARACTERS:
                omitted["trivial"] += 1
                continue
            user = row.user
            address = normalize_email(user.email) if user else None
            display_name = normalize_display_name(user.name if user else None) or address
            author = (
                OrganizationalEvidenceAuthor(
                    kind="internal_user", user_id=user.id,
                    display_name=display_name, address=address,
                ) if user and display_name and address
                else OrganizationalEvidenceAuthor(kind="authorless")
            )
            digest = _hash(normalized)
            content_key: tuple[str, str] | str = (
                (digest, _author_key(author))
                if self.preserve_separately_authored_content else digest
            )
            if content_key in seen_content:
                omitted["duplicate_content"] += 1
                continue
            seen_content.add(content_key)
            selected_text, truncated = _bounded(normalized, self.policy.maximum_item_characters)
            items.append(OrganizationalEvidenceItem(
                source_id=f"opportunity_note:{row.id}", source_type="note",
                occurred_at=row.created_at, updated_at=row.updated_at,
                text=selected_text, content_hash=_hash(selected_text), author=author,
                stable_identity={"internal_record_id": row.id},
                was_truncated=truncated, original_character_count=len(normalized),
            ))
        selected, selection_omissions = _select_items(items, self.policy)
        omitted.update(selection_omissions)
        return OrganizationalEvidenceCollection(
            items=tuple(selected), available_count=len(rows), selected_count=len(selected),
            omitted_reason_counts=dict(sorted(omitted.items())),
            total_selected_characters=sum(len(item.text) for item in selected),
            truncated=bool(len(rows) != len(selected) or any(item.was_truncated for item in selected)),
        )


def combine_team_summary_evidence(
    *, organization_id: int, workspace_id: int, opportunity_id: int,
    communications: OrganizationalEvidenceCollection,
    notes: OrganizationalEvidenceCollection,
    communication_policy: OrganizationalEvidenceSelectionPolicy,
    note_policy: OrganizationalEvidenceSelectionPolicy,
) -> TeamSummaryEvidenceBundle:
    """Combine selected sources without fuzzy matching or AI ranking."""
    ordered = sorted(
        (*communications.items, *notes.items),
        key=lambda item: (
            _time_key(item.occurred_at),
            0 if item.source_type == "note" else 1,
            item.source_id,
        ),
    )
    selected: list[OrganizationalEvidenceItem] = []
    seen: dict[tuple[str, str], OrganizationalEvidenceItem] = {}
    cross_omitted = Counter()
    for item in ordered:
        key = (item.content_hash, item.author.identity_fingerprint)
        existing = seen.get(key)
        if existing is None:
            seen[key] = item
            selected.append(item)
            continue
        if existing.source_type == "communication" and item.source_type == "note":
            selected[selected.index(existing)] = item
            seen[key] = item
            cross_omitted["cross_source_same_actor_duplicate"] += 1
        else:
            cross_omitted["cross_source_same_actor_duplicate"] += 1
    selected.sort(key=lambda item: (
        _time_key(item.occurred_at),
        0 if item.source_type == "note" else 1,
        item.source_id,
    ))
    omitted = Counter(communications.omitted_reason_counts)
    omitted.update(notes.omitted_reason_counts)
    omitted.update(cross_omitted)
    available_counts = {
        "communication": communications.available_count,
        "note": notes.available_count,
    }
    selected_counts = {
        "communication": sum(item.source_type == "communication" for item in selected),
        "note": sum(item.source_type == "note" for item in selected),
    }
    policies = {"communication": communication_policy, "note": note_policy}
    fingerprint_kwargs = {
        "organization_id": organization_id, "workspace_id": workspace_id,
        "opportunity_id": opportunity_id, "items": tuple(selected),
        "available_counts": available_counts, "selected_counts": selected_counts,
        "omitted_reason_counts": dict(sorted(omitted.items())),
        "selection_policies": policies,
        "truncated": communications.truncated or notes.truncated,
    }
    return TeamSummaryEvidenceBundle(
        **fingerprint_kwargs,
        total_selected_characters=sum(len(item.text) for item in selected),
        evidence_fingerprint=fingerprint_team_summary_evidence(**fingerprint_kwargs),
    )
