"""Database-independent contracts for Team Summary organizational evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Literal, Mapping


TEAM_SUMMARY_INPUT_CONTRACT_VERSION = "team-summary-evidence-v1"
TEAM_SUMMARY_SELECTION_POLICY_VERSION = "team-summary-selection-v1"
ActorKind = Literal["internal_user", "external_person", "authorless"]
SourceType = Literal["communication", "note"]


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class OrganizationalEvidenceAuthor:
    kind: ActorKind
    user_id: int | None = None
    display_name: str | None = None
    address: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"internal_user", "external_person", "authorless"}:
            raise ValueError("Unsupported organizational evidence author kind.")
        if self.kind == "internal_user" and (self.user_id is None or not self.address):
            raise ValueError("Internal authors require a user ID and normalized address.")
        if self.kind == "authorless" and any((self.user_id, self.display_name, self.address)):
            raise ValueError("Authorless evidence cannot carry identity fields.")
        if self.kind == "external_person" and self.user_id is not None:
            raise ValueError("External authors cannot carry an internal user ID.")

    @property
    def identity_fingerprint(self) -> str:
        return hashlib.sha256(_canonical(self.serializable_dict()).encode()).hexdigest()

    def serializable_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "user_id": self.user_id,
            "display_name": self.display_name,
            "address": self.address,
        }


@dataclass(frozen=True)
class OrganizationalEvidenceItem:
    source_id: str
    source_type: SourceType
    occurred_at: datetime | None
    updated_at: datetime | None
    text: str
    content_hash: str
    author: OrganizationalEvidenceAuthor
    title: str | None = None
    direction: str | None = None
    recipients: tuple[str, ...] = ()
    stable_identity: Mapping[str, Any] = field(default_factory=dict)
    was_truncated: bool = False
    original_character_count: int = 0

    def __post_init__(self) -> None:
        if self.source_type not in {"communication", "note"}:
            raise ValueError("Unsupported organizational evidence source type.")
        if not self.source_id or not self.text or not self.content_hash:
            raise ValueError("Evidence items require source ID, cleaned text, and content hash.")
        if self.original_character_count < len(self.text):
            raise ValueError("Original character count cannot be shorter than selected text.")

    def serializable_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        value = {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "occurred_at": _iso(self.occurred_at),
            "updated_at": _iso(self.updated_at),
            "content_hash": self.content_hash,
            "author": self.author.serializable_dict(),
            "title": self.title,
            "direction": self.direction,
            "recipients": list(self.recipients),
            "stable_identity": dict(sorted(self.stable_identity.items())),
            "was_truncated": self.was_truncated,
            "original_character_count": self.original_character_count,
        }
        if include_text:
            value["text"] = self.text
        return value

    def canonical_json(self, *, include_text: bool = True) -> str:
        return _canonical(self.serializable_dict(include_text=include_text))


@dataclass(frozen=True)
class OrganizationalEvidenceSelectionPolicy:
    maximum_count: int
    maximum_item_characters: int
    maximum_total_characters: int
    version: str = TEAM_SUMMARY_SELECTION_POLICY_VERSION

    def __post_init__(self) -> None:
        if min(self.maximum_count, self.maximum_item_characters, self.maximum_total_characters) <= 0:
            raise ValueError("Organizational evidence limits must be positive.")

    def serializable_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "maximum_count": self.maximum_count,
            "maximum_item_characters": self.maximum_item_characters,
            "maximum_total_characters": self.maximum_total_characters,
        }


@dataclass(frozen=True)
class OrganizationalEvidenceCollection:
    items: tuple[OrganizationalEvidenceItem, ...]
    available_count: int
    selected_count: int
    omitted_reason_counts: Mapping[str, int]
    total_selected_characters: int
    truncated: bool


@dataclass(frozen=True)
class TeamSummaryEvidenceBundle:
    organization_id: int
    workspace_id: int
    opportunity_id: int
    items: tuple[OrganizationalEvidenceItem, ...]
    available_counts: Mapping[str, int]
    selected_counts: Mapping[str, int]
    omitted_reason_counts: Mapping[str, int]
    selection_policies: Mapping[str, OrganizationalEvidenceSelectionPolicy]
    total_selected_characters: int
    truncated: bool
    evidence_fingerprint: str
    contract_version: str = TEAM_SUMMARY_INPUT_CONTRACT_VERSION

    def safe_fingerprint_payload(self) -> dict[str, Any]:
        """Return the exact content-free payload used by the fingerprint."""
        return team_summary_fingerprint_payload(
            organization_id=self.organization_id,
            workspace_id=self.workspace_id,
            opportunity_id=self.opportunity_id,
            items=self.items,
            available_counts=self.available_counts,
            selected_counts=self.selected_counts,
            omitted_reason_counts=self.omitted_reason_counts,
            selection_policies=self.selection_policies,
            truncated=self.truncated,
            contract_version=self.contract_version,
        )


def team_summary_fingerprint_payload(
    *, organization_id: int, workspace_id: int, opportunity_id: int,
    items: tuple[OrganizationalEvidenceItem, ...], available_counts: Mapping[str, int],
    selected_counts: Mapping[str, int], omitted_reason_counts: Mapping[str, int],
    selection_policies: Mapping[str, OrganizationalEvidenceSelectionPolicy],
    truncated: bool, contract_version: str = TEAM_SUMMARY_INPUT_CONTRACT_VERSION,
) -> dict[str, Any]:
    return {
        "contract_version": contract_version,
        "scope": {
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "opportunity_id": opportunity_id,
        },
        "items": [
            {
                "source_id": item.source_id,
                "source_type": item.source_type,
                "content_hash": item.content_hash,
                "occurred_at": _iso(item.occurred_at),
                "updated_at": _iso(item.updated_at),
                "author_identity_fingerprint": item.author.identity_fingerprint,
                "stable_identity_fingerprint": hashlib.sha256(
                    _canonical(dict(sorted(item.stable_identity.items()))).encode()
                ).hexdigest(),
                "was_truncated": item.was_truncated,
                "selected_character_count": len(item.text),
                "original_character_count": item.original_character_count,
            }
            for item in items
        ],
        "available_counts": dict(sorted(available_counts.items())),
        "selected_counts": dict(sorted(selected_counts.items())),
        "omitted_reason_counts": dict(sorted(omitted_reason_counts.items())),
        "selection_policies": {
            key: value.serializable_dict() for key, value in sorted(selection_policies.items())
        },
        "truncated": truncated,
    }


def fingerprint_team_summary_evidence(**kwargs: Any) -> str:
    return hashlib.sha256(_canonical(team_summary_fingerprint_payload(**kwargs)).encode()).hexdigest()
