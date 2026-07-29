from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ...models import Opportunity, OpportunityIntakeDraft, OpportunitySourceMaterial
from .contracts import INTAKE_SOURCE, IntakeCandidate


_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")


def normalize_duplicate_key(value: str | None) -> str | None:
    normalized = _NON_ALPHANUMERIC.sub("", str(value or "").lower())
    return normalized or None


@dataclass(frozen=True)
class DuplicateMatch:
    opportunity_id: int
    reason: str
    value: str


@dataclass(frozen=True)
class DuplicateCheckResult:
    exact_matches: tuple[DuplicateMatch, ...] = ()
    probable_matches: tuple[DuplicateMatch, ...] = ()

    @property
    def has_exact_match(self) -> bool:
        return bool(self.exact_matches)


def _deduplicate(matches: list[DuplicateMatch]) -> tuple[DuplicateMatch, ...]:
    seen: set[tuple[int, str, str]] = set()
    result: list[DuplicateMatch] = []
    for match in matches:
        key = (match.opportunity_id, match.reason, match.value)
        if key not in seen:
            seen.add(key)
            result.append(match)
    return tuple(result)


def find_publication_duplicates(
    db: Session,
    *,
    draft: OpportunityIntakeDraft,
    candidate: IntakeCandidate,
) -> DuplicateCheckResult:
    """Run tenant-scoped exact and probable duplicate checks for publication."""
    exact: list[DuplicateMatch] = []
    probable: list[DuplicateMatch] = []
    opportunities = db.query(Opportunity).filter(
        Opportunity.organization_id == draft.organization_id,
    ).all()

    solicitation_key = normalize_duplicate_key(candidate.solicitation_number)
    title_key = normalize_duplicate_key(candidate.title)
    client_key = normalize_duplicate_key(candidate.client)
    for opportunity in opportunities:
        if (
            solicitation_key
            and normalize_duplicate_key(opportunity.solicitation_number) == solicitation_key
        ):
            exact.append(DuplicateMatch(
                opportunity.id, "solicitation_number", candidate.solicitation_number or ""
            ))
        if (
            opportunity.source == INTAKE_SOURCE
            and opportunity.source_record_id == draft.internal_reference
        ):
            exact.append(DuplicateMatch(
                opportunity.id, "source_record_id", draft.internal_reference or ""
            ))
        if (
            title_key
            and client_key
            and normalize_duplicate_key(opportunity.title) == title_key
            and normalize_duplicate_key(opportunity.agency) == client_key
            and opportunity.response_deadline == candidate.response_deadline
        ):
            probable.append(DuplicateMatch(
                opportunity.id,
                "title_client_deadline",
                f"{candidate.title} | {candidate.client} | {candidate.response_deadline}",
            ))

    draft_materials = db.query(OpportunitySourceMaterial).filter(
        OpportunitySourceMaterial.organization_id == draft.organization_id,
        OpportunitySourceMaterial.workspace_id == draft.workspace_id,
        OpportunitySourceMaterial.intake_draft_id == draft.id,
    ).all()
    published_materials = db.query(OpportunitySourceMaterial).filter(
        OpportunitySourceMaterial.organization_id == draft.organization_id,
        OpportunitySourceMaterial.workspace_id == draft.workspace_id,
        OpportunitySourceMaterial.opportunity_id.isnot(None),
        or_(
            OpportunitySourceMaterial.intake_draft_id.is_(None),
            OpportunitySourceMaterial.intake_draft_id != draft.id,
        ),
    ).all()
    for material in draft_materials:
        for existing in published_materials:
            reason = value = None
            if material.sha256_digest and material.sha256_digest == existing.sha256_digest:
                reason, value = "source_material_sha256", material.sha256_digest
            elif (
                material.provider_message_id
                and normalize_duplicate_key(material.provider_message_id)
                == normalize_duplicate_key(existing.provider_message_id)
            ):
                reason, value = "provider_message_id", material.provider_message_id
            elif (
                material.internet_message_id
                and normalize_duplicate_key(material.internet_message_id)
                == normalize_duplicate_key(existing.internet_message_id)
            ):
                reason, value = "internet_message_id", material.internet_message_id
            if reason and value:
                exact.append(DuplicateMatch(existing.opportunity_id, reason, value))

    return DuplicateCheckResult(
        exact_matches=_deduplicate(exact),
        probable_matches=_deduplicate(probable),
    )
