"""Persistence operations for append-only Get Up to Speed generations.

This module deliberately contains no evidence collection, model invocation, prompt,
route, or presentation behavior.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Any, Iterable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ... import config
from ...models import (
    Opportunity,
    OpportunityKnowledgeBriefGeneration,
    OpportunityKnowledgeBriefSource,
    OpportunityKnowledgeBriefStatement,
    OpportunityKnowledgeBriefStatementSource,
    OrganizationMembership,
    User,
    Workspace,
)
from .constants import (
    AUTHORITIES,
    CONFIDENCE_VALUES,
    FAILURE_CATEGORIES,
    IMPORTANCE_VALUES,
    PLACEMENT_TYPES,
    REPRODUCIBILITY_STATUSES,
    SECTION_TYPES,
    SOURCE_CLASSES,
    FailureCategory,
    GenerationStatus,
    ReproducibilityStatus,
)


ACTIVE_STATUSES = (GenerationStatus.PENDING, GenerationStatus.RUNNING)
FINAL_STATUSES = (GenerationStatus.SUCCEEDED, GenerationStatus.FAILED)


class KnowledgeBriefPersistenceError(RuntimeError):
    """Base error for invalid GUTS persistence operations."""


class KnowledgeBriefScopeError(KnowledgeBriefPersistenceError):
    """Raised when organization, workspace, opportunity, or user scope conflicts."""


class KnowledgeBriefLifecycleError(KnowledgeBriefPersistenceError):
    """Raised when an append-only lifecycle transition is invalid."""


class KnowledgeBriefValidationError(KnowledgeBriefPersistenceError):
    """Raised before persistence when validated output references are inconsistent."""


class ActiveKnowledgeBriefGenerationError(KnowledgeBriefLifecycleError):
    """Raised when an opportunity already has a pending or running generation."""


_SOURCE_FIELDS = {
    "source_id", "source_class", "source_type", "authority", "verification", "title",
    "citation_label", "author_display_name", "author_user_id", "author_address", "provider",
    "occurred_at", "effective_at", "updated_at_source", "internal_model_name",
    "internal_record_id", "source_url", "filename", "content_hash", "parser_name",
    "parser_version", "retained_by_bidlens", "selected_character_count",
    "original_character_count", "was_truncated",
}
_SOURCE_REQUIRED_FIELDS = {"source_id", "source_class", "source_type", "authority", "citation_label"}
_STATEMENT_FIELDS = {
    "statement_key", "placement_type", "section_type", "section_title", "position", "text",
    "importance", "confidence",
}
_STATEMENT_REQUIRED_FIELDS = {
    "statement_key", "placement_type", "position", "text", "importance", "confidence",
}
_SUCCESS_METADATA_FIELDS = {
    "manifest_hash", "source_snapshot_started_at", "source_snapshot_completed_at", "latest_source_at",
    "source_summary_json", "warning_metadata_json", "statistics_json", "input_character_count",
    "estimated_input_tokens", "input_tokens", "output_tokens", "total_tokens",
    "validation_retry_count", "authorization_ms", "current_state_ms", "official_evidence_ms",
    "communication_ms", "notes_ms", "history_ms", "manifest_ms", "model_ms", "validation_ms",
    "persistence_ms", "total_ms", "degraded_source_count", "input_truncated", "provider", "model",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _validate_scope(
    db: Session,
    *,
    organization_id: int,
    workspace_id: int,
    opportunity_id: int,
    generated_by_user_id: int,
) -> None:
    opportunity = db.get(Opportunity, opportunity_id)
    workspace = db.get(Workspace, workspace_id)
    user = db.get(User, generated_by_user_id)
    membership = db.query(OrganizationMembership.id).filter(
        OrganizationMembership.organization_id == organization_id,
        OrganizationMembership.user_id == generated_by_user_id,
    ).first()
    if opportunity is None or opportunity.organization_id != organization_id:
        raise KnowledgeBriefScopeError("Opportunity does not belong to the requested organization.")
    if workspace is None or workspace.organization_id != organization_id:
        raise KnowledgeBriefScopeError("Workspace does not belong to the requested organization.")
    if user is None or membership is None:
        raise KnowledgeBriefScopeError("Generating user is not a member of the requested organization.")


def get_active_generation(
    db: Session,
    *,
    organization_id: int,
    opportunity_id: int,
) -> OpportunityKnowledgeBriefGeneration | None:
    return db.query(OpportunityKnowledgeBriefGeneration).filter(
        OpportunityKnowledgeBriefGeneration.organization_id == organization_id,
        OpportunityKnowledgeBriefGeneration.opportunity_id == opportunity_id,
        OpportunityKnowledgeBriefGeneration.status.in_(ACTIVE_STATUSES),
    ).order_by(
        OpportunityKnowledgeBriefGeneration.requested_at.desc(),
        OpportunityKnowledgeBriefGeneration.id.desc(),
    ).first()


def create_pending_generation(
    db: Session,
    *,
    organization_id: int,
    workspace_id: int,
    opportunity_id: int,
    generated_by_user_id: int,
    requested_at: datetime | None = None,
) -> OpportunityKnowledgeBriefGeneration:
    _validate_scope(
        db,
        organization_id=organization_id,
        workspace_id=workspace_id,
        opportunity_id=opportunity_id,
        generated_by_user_id=generated_by_user_id,
    )
    if get_active_generation(db, organization_id=organization_id, opportunity_id=opportunity_id):
        raise ActiveKnowledgeBriefGenerationError("A generation is already active for this opportunity.")

    generation = OpportunityKnowledgeBriefGeneration(
        organization_id=organization_id,
        workspace_id=workspace_id,
        opportunity_id=opportunity_id,
        generated_by_user_id=generated_by_user_id,
        status=GenerationStatus.PENDING,
        requested_at=requested_at or _utcnow(),
        prompt_version=config.GUTS_PROMPT_VERSION,
        manifest_version=config.GUTS_MANIFEST_VERSION,
        output_schema_version=config.GUTS_OUTPUT_SCHEMA_VERSION,
        provider=config.GUTS_AI_PROVIDER,
        model=config.GUTS_AI_MODEL,
        reproducibility_status=ReproducibilityStatus.NOT_REPRODUCIBLE,
    )
    db.add(generation)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ActiveKnowledgeBriefGenerationError("A generation is already active for this opportunity.") from exc
    db.refresh(generation)
    return generation


def mark_generation_running(
    db: Session,
    generation: OpportunityKnowledgeBriefGeneration,
    *,
    started_at: datetime | None = None,
) -> OpportunityKnowledgeBriefGeneration:
    if generation.status != GenerationStatus.PENDING:
        raise KnowledgeBriefLifecycleError("Only a pending generation can be marked running.")
    generation.status = GenerationStatus.RUNNING
    generation.started_at = started_at or _utcnow()
    db.commit()
    db.refresh(generation)
    return generation


def update_active_generation_metadata(
    db: Session,
    generation: OpportunityKnowledgeBriefGeneration,
    *,
    metadata: dict[str, Any],
) -> OpportunityKnowledgeBriefGeneration:
    """Persist bounded lifecycle metadata without finalizing an active attempt."""
    if generation.status not in ACTIVE_STATUSES:
        raise KnowledgeBriefLifecycleError("Only an active generation can receive lifecycle metadata.")
    unknown = set(metadata) - _SUCCESS_METADATA_FIELDS
    if unknown:
        raise KnowledgeBriefValidationError(f"Unsupported generation metadata fields: {sorted(unknown)}")
    for key, value in metadata.items():
        setattr(generation, key, value)
    db.commit()
    db.refresh(generation)
    return generation


def mark_generation_failed(
    db: Session,
    generation: OpportunityKnowledgeBriefGeneration,
    *,
    failure_category: str,
    failure_stage: str | None = None,
    safe_error_message: str | None = None,
    completed_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> OpportunityKnowledgeBriefGeneration:
    if generation.status not in ACTIVE_STATUSES:
        raise KnowledgeBriefLifecycleError("A completed generation cannot be reopened or changed.")
    if failure_category not in FAILURE_CATEGORIES:
        raise KnowledgeBriefValidationError(f"Unsupported failure category: {failure_category}")
    for key, value in (metadata or {}).items():
        if key not in _SUCCESS_METADATA_FIELDS:
            raise KnowledgeBriefValidationError(f"Unsupported failure metadata field: {key}")
        setattr(generation, key, value)
    generation.status = GenerationStatus.FAILED
    generation.failure_category = failure_category
    generation.failure_stage = failure_stage
    generation.safe_error_message = (safe_error_message or "")[:500] or None
    generation.completed_at = completed_at or _utcnow()
    db.commit()
    db.refresh(generation)
    return generation


def _validated_rows(
    sources: Iterable[dict[str, Any]],
    statements: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source_rows = [dict(row) for row in sources]
    statement_rows = [dict(row) for row in statements]
    source_ids = [row.get("source_id") for row in source_rows]
    statement_keys = [row.get("statement_key") for row in statement_rows]
    if any(not value for value in source_ids) or len(source_ids) != len(set(source_ids)):
        raise KnowledgeBriefValidationError("Source IDs must be present and unique within a generation.")
    if any(not value for value in statement_keys) or len(statement_keys) != len(set(statement_keys)):
        raise KnowledgeBriefValidationError("Statement keys must be present and unique within a generation.")

    source_id_set = set(source_ids)
    for row in source_rows:
        unknown = set(row) - _SOURCE_FIELDS
        missing = _SOURCE_REQUIRED_FIELDS - {key for key, value in row.items() if value not in (None, "")}
        if unknown or missing:
            raise KnowledgeBriefValidationError(
                f"Invalid source fields; unknown={sorted(unknown)} missing={sorted(missing)}"
            )
        if row["source_class"] not in SOURCE_CLASSES or row["authority"] not in AUTHORITIES:
            raise KnowledgeBriefValidationError(f"Source {row['source_id']} uses unsupported contract values.")
    for row in statement_rows:
        citations = row.pop("source_ids", None)
        unknown = set(row) - _STATEMENT_FIELDS
        missing = _STATEMENT_REQUIRED_FIELDS - {key for key, value in row.items() if value not in (None, "")}
        if unknown or missing:
            raise KnowledgeBriefValidationError(
                f"Invalid statement fields; unknown={sorted(unknown)} missing={sorted(missing)}"
            )
        if row["placement_type"] not in PLACEMENT_TYPES:
            raise KnowledgeBriefValidationError(f"Statement {row['statement_key']} has unsupported placement.")
        if row.get("section_type") is not None and row["section_type"] not in SECTION_TYPES:
            raise KnowledgeBriefValidationError(f"Statement {row['statement_key']} has unsupported section type.")
        if row["importance"] not in IMPORTANCE_VALUES or row["confidence"] not in CONFIDENCE_VALUES:
            raise KnowledgeBriefValidationError(f"Statement {row['statement_key']} uses unsupported contract values.")
        if not isinstance(citations, list) or not citations:
            raise KnowledgeBriefValidationError(f"Statement {row.get('statement_key')} requires at least one citation.")
        if len(citations) != len(set(citations)):
            raise KnowledgeBriefValidationError(f"Statement {row.get('statement_key')} has duplicate citations.")
        unknown_sources = set(citations) - source_id_set
        if unknown_sources:
            raise KnowledgeBriefValidationError(
                f"Statement {row.get('statement_key')} references unknown sources: {sorted(unknown_sources)}"
            )
        row["_source_ids"] = citations
    return source_rows, statement_rows


def save_generation_success(
    db: Session,
    generation: OpportunityKnowledgeBriefGeneration,
    *,
    output_json: dict[str, Any],
    current_state_snapshot_json: dict[str, Any],
    sources: Iterable[dict[str, Any]],
    statements: Iterable[dict[str, Any]],
    reproducibility_status: str,
    completed_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
    persistence_started_monotonic: float | None = None,
    total_started_monotonic: float | None = None,
) -> OpportunityKnowledgeBriefGeneration:
    if generation.status != GenerationStatus.RUNNING:
        raise KnowledgeBriefLifecycleError("Only a running generation can succeed.")
    if not isinstance(output_json, dict) or not isinstance(current_state_snapshot_json, dict):
        raise KnowledgeBriefValidationError("Validated output and current-state snapshot must be JSON objects.")
    if reproducibility_status not in REPRODUCIBILITY_STATUSES:
        raise KnowledgeBriefValidationError(f"Unsupported reproducibility status: {reproducibility_status}")
    source_rows, statement_rows = _validated_rows(sources, statements)
    unknown_metadata = set(metadata or {}) - _SUCCESS_METADATA_FIELDS
    if unknown_metadata:
        raise KnowledgeBriefValidationError(f"Unsupported success metadata fields: {sorted(unknown_metadata)}")

    try:
        source_models: dict[str, OpportunityKnowledgeBriefSource] = {}
        for row in source_rows:
            source = OpportunityKnowledgeBriefSource(generation=generation, **row)
            db.add(source)
            source_models[row["source_id"]] = source
        db.flush()

        for row in statement_rows:
            citation_ids = row.pop("_source_ids")
            statement = OpportunityKnowledgeBriefStatement(generation=generation, **row)
            db.add(statement)
            db.flush()
            for position, source_id in enumerate(citation_ids):
                db.add(OpportunityKnowledgeBriefStatementSource(
                    statement=statement,
                    brief_source=source_models[source_id],
                    position=position,
                ))

        for key, value in (metadata or {}).items():
            setattr(generation, key, value)
        generation.output_json = output_json
        generation.current_state_snapshot_json = current_state_snapshot_json
        generation.reproducibility_status = reproducibility_status
        generation.status = GenerationStatus.SUCCEEDED
        generation.completed_at = completed_at or _utcnow()
        generation.failure_category = None
        generation.failure_stage = None
        generation.safe_error_message = None
        # These timings cover application-side persistence preparation and
        # flush work through the point immediately before the final commit.
        now_monotonic = perf_counter()
        if persistence_started_monotonic is not None:
            generation.persistence_ms = round((now_monotonic - persistence_started_monotonic) * 1000)
        if total_started_monotonic is not None:
            generation.total_ms = round((now_monotonic - total_started_monotonic) * 1000)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(generation)
    return generation


def get_latest_successful_generation(
    db: Session,
    *,
    organization_id: int,
    opportunity_id: int,
    eager: bool = True,
) -> OpportunityKnowledgeBriefGeneration | None:
    query = db.query(OpportunityKnowledgeBriefGeneration)
    if eager:
        query = query.options(
            selectinload(OpportunityKnowledgeBriefGeneration.sources),
            selectinload(OpportunityKnowledgeBriefGeneration.statements)
            .selectinload(OpportunityKnowledgeBriefStatement.source_links)
            .selectinload(OpportunityKnowledgeBriefStatementSource.brief_source),
        )
    return query.filter(
        OpportunityKnowledgeBriefGeneration.organization_id == organization_id,
        OpportunityKnowledgeBriefGeneration.opportunity_id == opportunity_id,
        OpportunityKnowledgeBriefGeneration.status == GenerationStatus.SUCCEEDED,
    ).order_by(
        OpportunityKnowledgeBriefGeneration.completed_at.desc(),
        OpportunityKnowledgeBriefGeneration.id.desc(),
    ).first()


def expire_stale_generation(
    db: Session,
    generation: OpportunityKnowledgeBriefGeneration,
    *,
    max_age_seconds: int,
    now: datetime | None = None,
) -> bool:
    if generation.status not in ACTIVE_STATUSES:
        return False
    current_time = now or _utcnow()
    requested_at = generation.requested_at
    if requested_at.tzinfo is None:
        requested_at = requested_at.replace(tzinfo=timezone.utc)
    if requested_at > current_time - timedelta(seconds=max_age_seconds):
        return False
    mark_generation_failed(
        db,
        generation,
        failure_category=FailureCategory.STALE_ATTEMPT,
        failure_stage="lifecycle",
        safe_error_message="The generation attempt expired before completion.",
        completed_at=current_time,
    )
    return True
