"""Strict, database-free domain contracts for Get Up to Speed."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .attribution import actor_identity_key, normalize_display_name, normalize_email


SourceClassValue = Literal[
    "current_state", "official_evidence", "organizational_knowledge", "historical_context"
]
AuthorityValue = Literal[
    "authoritative_current", "official_source", "attributed_claim", "historical_record"
]
PlacementValue = Literal["headline", "summary", "section"]
SectionValue = Literal[
    "current_state", "official_updates", "organizational_knowledge", "important_history", "uncertainties"
]
ImportanceValue = Literal["high", "normal"]
ConfidenceValue = Literal["supported", "attributed", "uncertain"]
WarningValue = Literal[
    "missing_source", "partial_generation", "conflicting_sources", "truncated_input", "not_fully_reproducible"
]
ReproducibilityValue = Literal[
    "fully_reproducible", "partially_reproducible", "not_reproducible"
]
ConflictResolutionValue = Literal[
    "authoritative_current_wins", "newer_official_source_wins", "unresolved_internal_disagreement"
]
FailureCategoryValue = Literal[
    "access_denied", "shortlist_required", "opportunity_not_found", "source_collection_failed",
    "source_retrieval_failed", "source_parse_failed", "insufficient_evidence", "manifest_build_failed",
    "manifest_validation_failed", "model_configuration_missing", "model_timeout", "model_provider_error",
    "model_schema_invalid", "model_citation_invalid", "model_output_unsafe", "persistence_failed",
    "generation_already_in_progress", "stale_attempt", "unexpected_error",
]


def _explicit_json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return normalized.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return {key: _explicit_json_value(item) for key, item in value.model_dump(mode="python").items()}
    if isinstance(value, dict):
        return {str(key): _explicit_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_explicit_json_value(item) for item in value]
    return value


class GUTSContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    def serializable_dict(self) -> dict[str, Any]:
        return _explicit_json_value(self)

    def canonical_json(self) -> str:
        return json.dumps(self.serializable_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class EvidenceAuthor(GUTSContract):
    user_id: int | None = None
    display_name: str | None = None
    address: str | None = None

    @field_validator("display_name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        return normalize_display_name(value)

    @field_validator("address")
    @classmethod
    def normalize_address(cls, value: str | None) -> str | None:
        return normalize_email(value)


class AttributionActor(GUTSContract):
    user_id: int | None
    display_name: str | None
    email: str | None

    @field_validator("user_id")
    @classmethod
    def validate_user_id(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("user_id must be positive")
        return value

    @field_validator("display_name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        return normalize_display_name(value)

    @field_validator("email")
    @classmethod
    def normalize_actor_email(cls, value: str | None) -> str | None:
        return normalize_email(value)

    @model_validator(mode="after")
    def validate_identity(self) -> "AttributionActor":
        if self.user_id is not None and (self.display_name is None or self.email is None):
            raise ValueError("internal actors require complete snapshots")
        if self.user_id is None and self.display_name is None and self.email is None:
            raise ValueError("external actors require a display name or email")
        return self


class StatementAttribution(GUTSContract):
    type: Literal["person", "internal_source"]
    actors: tuple[AttributionActor, ...]

    @model_validator(mode="after")
    def validate_actor_set(self) -> "StatementAttribution":
        if self.type == "internal_source" and self.actors:
            raise ValueError("internal_source attribution cannot contain actors")
        if self.type == "person" and not 1 <= len(self.actors) <= 10:
            raise ValueError("person attribution requires one to ten actors")
        keys = [actor_identity_key(actor) for actor in self.actors]
        if None in keys or len(keys) != len(set(keys)):
            raise ValueError("attribution actors must be unique")
        return self


class CurrentStateField(GUTSContract):
    value: str | int | bool | None
    source_id: str


class OrganizationOutcomeState(GUTSContract):
    outcome_type: Literal["bidding", "no_bid"]
    recorded_at: datetime
    recorded_by_user_id: int
    recorded_by_display_name: str


class InterestedTeammate(GUTSContract):
    user_id: int
    display_name: str


class SalesforceLinkState(GUTSContract):
    linked: bool
    url: str | None = None


class CurrentOpportunityState(GUTSContract):
    opportunity_id: int
    organization_id: int
    workspace_id: int
    title: CurrentStateField
    client: CurrentStateField
    description: CurrentStateField
    response_deadline: CurrentStateField
    posted_date: CurrentStateField
    solicitation_number: CurrentStateField
    opportunity_type: CurrentStateField
    source_stage: CurrentStateField
    source: CurrentStateField
    source_record_id: CurrentStateField
    source_url: CurrentStateField
    sam_url: CurrentStateField
    bidlens_id: CurrentStateField
    sam_notice_id: CurrentStateField
    naics: CurrentStateField
    naics_title: CurrentStateField
    set_aside: CurrentStateField
    description_original_character_count: int
    description_was_truncated: bool
    outcome: OrganizationOutcomeState | None = None
    interested_teammates: tuple[InterestedTeammate, ...] = ()
    salesforce: SalesforceLinkState


class EvidenceSource(GUTSContract):
    source_id: str
    source_class: SourceClassValue
    source_type: str
    authority: AuthorityValue
    citation_label: str
    text: str
    author: EvidenceAuthor | None = None
    occurred_at: datetime | None = None
    effective_at: datetime | None = None
    content_hash: str | None = None
    was_truncated: bool = False
    title: str | None = None
    updated_at_source: datetime | None = None
    internal_model_name: str | None = None
    internal_record_id: int | None = None
    selected_character_count: int = Field(default=0, ge=0)
    original_character_count: int = Field(default=0, ge=0)
    provenance: dict[str, Any] = Field(default_factory=dict)
    verification: str | None = None
    provider: str | None = None
    source_url: str | None = None
    filename: str | None = None
    parser_name: str | None = None
    parser_version: str | None = None
    retained_by_bidlens: bool = True
    untrusted_data: bool = True
    structured_facts: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_character_counts(self) -> "EvidenceSource":
        if self.selected_character_count != len(self.text):
            raise ValueError("selected_character_count must equal evidence text length")
        if self.original_character_count < self.selected_character_count:
            raise ValueError("original_character_count cannot be smaller than selected content")
        return self


class EvidenceCollectionResult(GUTSContract):
    evidence: tuple[EvidenceSource, ...] = ()
    available_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    truncated: bool
    omitted_reason_counts: dict[str, int] = Field(default_factory=dict)
    latest_source_at: datetime | None = None
    total_selected_characters: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_internal_consistency(self) -> "EvidenceCollectionResult":
        if self.selected_count != len(self.evidence):
            raise ValueError("selected_count must equal the evidence length")
        if self.available_count != self.selected_count + self.excluded_count:
            raise ValueError("available_count must equal selected_count plus excluded_count")
        if sum(self.omitted_reason_counts.values()) != self.excluded_count:
            raise ValueError("omission diagnostics must account for every excluded source")
        selected_characters = sum(source.selected_character_count for source in self.evidence)
        if self.total_selected_characters != selected_characters:
            raise ValueError("total_selected_characters must equal selected evidence characters")
        latest = max((source.occurred_at for source in self.evidence if source.occurred_at), default=None)
        if self.latest_source_at != latest:
            raise ValueError("latest_source_at must match selected evidence")
        if self.truncated != bool(self.excluded_count or any(source.was_truncated for source in self.evidence)):
            raise ValueError("truncated must reflect exclusions or per-source truncation")
        return self


class OfficialEvidenceCollectionResult(EvidenceCollectionResult):
    unavailable_sources: tuple["UnavailableSource", ...] = ()
    contains_unretained_external: bool = False


class UnavailableSource(GUTSContract):
    source_id: str
    source_type: str
    failure_category: FailureCategoryValue
    safe_message: str
    retryable: bool
    provenance: dict[str, Any] = Field(default_factory=dict)


class KnownConflict(GUTSContract):
    conflict_id: str
    field_name: str
    authoritative_value: str | int | bool | None
    authoritative_source_id: str
    conflicting_value: str | int | bool | None
    conflicting_source_id: str
    resolution: ConflictResolutionValue
    material: bool = True
    include_in_briefing: bool = False


class EvidenceSelectionStatistics(GUTSContract):
    available_source_count: int = Field(ge=0)
    unavailable_source_count: int = Field(ge=0)
    selected_character_count: int = Field(ge=0)
    original_character_count: int = Field(ge=0)
    truncated_source_count: int = Field(ge=0)


class EvidenceSelection(GUTSContract):
    sources: tuple[EvidenceSource, ...] = ()
    unavailable_sources: tuple[UnavailableSource, ...] = ()
    known_conflicts: tuple[KnownConflict, ...] = ()
    statistics: EvidenceSelectionStatistics


class FinalEvidenceSelection(GUTSContract):
    selection: EvidenceSelection
    latest_source_at: datetime | None = None
    reproducibility_status: ReproducibilityValue
    omitted_reason_counts: dict[str, int] = Field(default_factory=dict)
    input_truncated: bool = False


class GenerationConstraints(GUTSContract):
    max_total_input_characters: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0)
    max_retries: int = Field(ge=0)


class SystemWarning(GUTSContract):
    warning_type: WarningValue
    message: str
    source_ids: tuple[str, ...] = ()


class GUTSManifest(GUTSContract):
    manifest_version: str
    opportunity_id: int
    organization_id: int
    workspace_id: int
    snapshot_started_at: datetime
    snapshot_completed_at: datetime
    current_state: CurrentOpportunityState
    evidence: EvidenceSelection
    constraints: GenerationConstraints
    warnings: tuple[SystemWarning, ...] = ()
    reproducibility_status: ReproducibilityValue
    briefing_goal: str = "Provide an accurate, concise, citation-backed opportunity briefing."

    def allowed_source_ids(self) -> tuple[str, ...]:
        current_ids = tuple(
            getattr(self.current_state, name).source_id
            for name in sorted(KNOWN_CURRENT_STATE_FIELDS - {"agency"})
        )
        outcome_ids = (
            (f"current_state:opportunity:{self.opportunity_id}:organization_outcome",)
            if self.current_state.outcome else ()
        )
        return tuple(sorted((*current_ids, *outcome_ids, *(source.source_id for source in self.evidence.sources))))

    def required_current_state_citations(self) -> dict[str, str]:
        return {
            name: getattr(self.current_state, name).source_id
            for name in ("response_deadline", "solicitation_number", "source_stage")
        }


class ModelStatement(GUTSContract):
    statement_key: str
    placement_type: PlacementValue
    section_type: SectionValue | None = None
    position: int = Field(ge=0)
    text: str
    importance: ImportanceValue
    confidence: ConfidenceValue
    source_ids: tuple[str, ...]
    attribution: StatementAttribution | None = None

    @field_validator("source_ids")
    @classmethod
    def require_citations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("Statements require unique source citations")
        return value


class ModelSection(GUTSContract):
    section_type: SectionValue
    title: str
    position: int = Field(ge=0)
    statements: tuple[ModelStatement, ...]


class ModelOutputStatement(GUTSContract):
    statement_key: str
    text: str
    importance: ImportanceValue
    confidence: ConfidenceValue
    source_ids: tuple[str, ...]
    attribution: StatementAttribution | None = None

    @field_validator("source_ids")
    @classmethod
    def require_output_citations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("Statements require citations")
        return value


class ProviderOutputStatementV2(GUTSContract):
    """Keyless statement shape returned by the V2 model provider."""

    text: str
    importance: ImportanceValue
    confidence: ConfidenceValue
    source_ids: tuple[str, ...]
    attribution: StatementAttribution | None

    @field_validator("source_ids")
    @classmethod
    def require_output_citations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("Statements require citations")
        return value


class ModelOutputSection(GUTSContract):
    section_type: SectionValue
    statements: tuple[ModelOutputStatement, ...]


class ModelBriefingOutput(GUTSContract):
    headline: ModelOutputStatement
    summary_statements: tuple[ModelOutputStatement, ...]
    sections: tuple[ModelOutputSection, ...]


class ProviderOutputSectionV2(GUTSContract):
    section_type: SectionValue
    statements: tuple[ProviderOutputStatementV2, ...]


class ProviderBriefingOutputV2(GUTSContract):
    headline: ProviderOutputStatementV2
    summary_statements: tuple[ProviderOutputStatementV2, ...]
    sections: tuple[ProviderOutputSectionV2, ...]


class ValidatedModelBriefing(GUTSContract):
    headline: ModelStatement
    summary: tuple[ModelStatement, ...]
    sections: tuple[ModelSection, ...]


class ValidatedBriefingOutput(GUTSContract):
    output_schema_version: str
    briefing: ValidatedModelBriefing
    validated_at: datetime


KNOWN_CURRENT_STATE_FIELDS = frozenset({
    "title", "agency", "client", "description", "response_deadline", "posted_date",
    "solicitation_number", "opportunity_type", "source_stage", "source", "source_record_id",
    "source_url", "sam_url", "bidlens_id", "sam_notice_id", "naics", "naics_title", "set_aside",
})


def current_state_source_id(opportunity_id: int, field_name: str) -> str:
    if isinstance(opportunity_id, bool) or not isinstance(opportunity_id, int) or opportunity_id <= 0:
        raise ValueError("opportunity_id must be a positive integer")
    if field_name not in KNOWN_CURRENT_STATE_FIELDS:
        raise ValueError("field_name is not a controlled current-state field")
    return f"current_state:opportunity:{opportunity_id}:{field_name}"
