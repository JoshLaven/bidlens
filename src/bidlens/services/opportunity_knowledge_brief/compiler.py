"""Compiler lifecycle for one append-only Get Up to Speed generation."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
from time import perf_counter
from typing import Any, Callable

from sqlalchemy.orm import Session

from ... import config
from ...models import OpportunityKnowledgeBriefGeneration
from .access_policy import GUTSAccessContext
from .constants import FailureCategory, GenerationStatus
from .contracts import (
    CurrentOpportunityState, EvidenceCollectionResult, EvidenceSource,
    FinalEvidenceSelection, GenerationConstraints, OfficialEvidenceCollectionResult,
)
from .current_state import CurrentStateAssembler
from .historical_evidence import HistoricalEvidenceCollector
from .manifest import ManifestBuilder, ManifestHasher, ManifestValidationError
from .model_client import GUTSModelError, generate_validated_briefing
from .official_evidence import OfficialEvidenceCollector
from .organizational_evidence import CommunicationEvidenceCollector, NoteEvidenceCollector
from .repository import (
    KnowledgeBriefPersistenceError, mark_generation_failed, mark_generation_running,
    save_generation_success, update_active_generation_metadata,
)
from .selection import ConflictDetector, EvidenceSelector


logger = logging.getLogger(__name__)


class GUTSCompilerError(RuntimeError):
    def __init__(
        self, safe_category: str, safe_message: str, *, stage: str,
        retryable: bool = False, validation_debug: dict[str, Any] | None = None,
        provider_debug: dict[str, Any] | None = None,
        schema_debug: dict[str, Any] | None = None,
    ):
        super().__init__(safe_message)
        self.safe_category = safe_category
        self.safe_message = safe_message
        self.stage = stage
        self.retryable = retryable
        self.validation_debug = validation_debug
        self.provider_debug = provider_debug
        self.schema_debug = schema_debug


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _elapsed_ms(started: float) -> int:
    return round((perf_counter() - started) * 1000)


def _meaningful(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def has_minimum_evidence(state: CurrentOpportunityState) -> bool:
    title = _meaningful(state.title.value)
    if not title or title.casefold() in {
        "untitled", "untitled opportunity", "new opportunity", "unknown", "tbd", "n/a", "none",
    }:
        return False
    context = (
        _meaningful(state.client.value), _meaningful(state.response_deadline.value),
        _meaningful(state.source_stage.value), _meaningful(state.solicitation_number.value),
        _meaningful(state.description.value),
    )
    return any(value.casefold() not in {"unknown", "tbd", "n/a", "none"} for value in context if value)


def _hash_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _current_state_sources(state: CurrentOpportunityState) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    fields = (
        "title", "client", "description", "response_deadline", "posted_date", "solicitation_number",
        "opportunity_type", "source_stage", "source", "source_record_id", "source_url", "sam_url",
        "bidlens_id", "sam_notice_id", "naics", "naics_title", "set_aside",
    )
    for name in fields:
        field = getattr(state, name)
        serialized = {"field": name, "value": field.value}
        value_length = len(_meaningful(field.value))
        rows.append({
            "source_id": field.source_id, "source_class": "current_state", "source_type": name,
            "authority": "authoritative_current", "citation_label": f"Current opportunity: {name.replace('_', ' ')}",
            "internal_model_name": "Opportunity", "internal_record_id": str(state.opportunity_id),
            "content_hash": _hash_json(serialized), "retained_by_bidlens": True,
            "selected_character_count": value_length, "original_character_count": (
                state.description_original_character_count if name == "description" else value_length
            ), "was_truncated": state.description_was_truncated if name == "description" else False,
        })
    if state.outcome:
        rows.append({
            "source_id": f"current_state:opportunity:{state.opportunity_id}:organization_outcome",
            "source_class": "current_state", "source_type": "organization_outcome",
            "authority": "authoritative_current", "citation_label": "Current organization outcome",
            "internal_model_name": "OpportunityOutcome", "internal_record_id": str(state.opportunity_id),
            "content_hash": _hash_json(state.outcome.serializable_dict()), "retained_by_bidlens": True,
            "selected_character_count": len(state.outcome.canonical_json()),
            "original_character_count": len(state.outcome.canonical_json()), "was_truncated": False,
        })
    return rows


def _evidence_source_row(source: EvidenceSource) -> dict[str, Any]:
    author = source.author
    return {
        "source_id": source.source_id, "source_class": source.source_class,
        "source_type": source.source_type, "authority": source.authority,
        "verification": source.verification, "title": source.title,
        "citation_label": source.citation_label,
        "author_display_name": author.display_name if author else None,
        "author_user_id": author.user_id if author else None,
        "author_address": author.address if author else None,
        "provider": source.provider, "occurred_at": source.occurred_at,
        "effective_at": source.effective_at, "updated_at_source": source.updated_at_source,
        "internal_model_name": source.internal_model_name,
        "internal_record_id": str(source.internal_record_id) if source.internal_record_id is not None else None,
        "source_url": source.source_url, "filename": source.filename,
        "content_hash": source.content_hash, "parser_name": source.parser_name,
        "parser_version": source.parser_version, "retained_by_bidlens": source.retained_by_bidlens,
        "selected_character_count": source.selected_character_count,
        "original_character_count": source.original_character_count,
        "was_truncated": source.was_truncated,
    }


def _statement_rows(validated) -> list[dict[str, Any]]:
    briefing = validated.briefing
    statements = [briefing.headline, *briefing.summary, *(item for section in briefing.sections for item in section.statements)]
    section_titles = {section.section_type: section.title for section in briefing.sections}
    return [{
        "statement_key": item.statement_key, "placement_type": item.placement_type,
        "section_type": item.section_type,
        "section_title": section_titles.get(item.section_type) if item.section_type else None,
        "position": item.position, "text": item.text, "importance": item.importance,
        "confidence": item.confidence, "source_ids": list(item.source_ids),
    } for item in statements]


def _communication_coverage(selection: FinalEvidenceSelection, validated) -> dict[str, int]:
    communication_ids = {
        source.source_id for source in selection.selection.sources if source.source_type == "email"
    }
    briefing = validated.briefing
    statements = (
        briefing.headline, *briefing.summary,
        *(statement for section in briefing.sections for statement in section.statements),
    )
    represented = sum(bool(communication_ids.intersection(statement.source_ids)) for statement in statements)
    return {
        "selected_communications": len(communication_ids),
        "communication_derived_statements": represented,
    }


def _warnings(
    selection: FinalEvidenceSelection, *, input_truncated: bool,
    communication_coverage: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    unavailable = selection.selection.unavailable_sources
    if unavailable:
        warnings.append({"type": "missing_source", "text": "Briefing generated from available information. One or more official sources could not be used.", "metadata": {"count": len(unavailable)}})
        warnings.append({"type": "partial_generation", "text": "Some official source information was unavailable during generation.", "metadata": {"unavailable_official_sources": len(unavailable)}})
    conflicts = [item for item in selection.selection.known_conflicts if item.material and item.include_in_briefing]
    if conflicts:
        warnings.append({"type": "conflicting_sources", "text": "The selected evidence contains a material unresolved conflict.", "metadata": {"count": len(conflicts)}})
    if input_truncated:
        warnings.append({"type": "truncated_input", "text": "Some available evidence was omitted or shortened to fit generation limits.", "metadata": {"omitted_count": sum(selection.omitted_reason_counts.values())}})
    if selection.reproducibility_status != "fully_reproducible":
        warnings.append({"type": "not_fully_reproducible", "text": "This briefing used selected external evidence that is not fully retained by BidLens.", "metadata": {"status": selection.reproducibility_status}})
    coverage = communication_coverage or {}
    if coverage.get("selected_communications", 0) > 0 and coverage.get("communication_derived_statements", 0) == 0:
        warnings.append({
            "type": "communication_evidence_unused",
            "text": "Selected communication evidence was not represented in the generated briefing.",
            "visibility": "development",
            "metadata": dict(coverage),
        })
    return warnings


def _summaries(
    selection: FinalEvidenceSelection, official: OfficialEvidenceCollectionResult,
    notes: EvidenceCollectionResult, communications: EvidenceCollectionResult,
    history: EvidenceCollectionResult, *, input_truncated: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    selected = selection.selection.sources
    used = {
        "official_documents": sum(source.source_class == "official_evidence" for source in selected),
        "communications": sum(source.source_type == "email" for source in selected),
        "notes": sum(source.source_type == "note" for source in selected),
        "history_events": sum(source.source_class == "historical_context" for source in selected),
    }
    source_summary = {
        "official_documents": {"available": official.available_count, "used": used["official_documents"], "failed": len(official.unavailable_sources)},
        "communications": {"available": communications.available_count, "used": used["communications"]},
        "notes": {"available": notes.available_count, "used": used["notes"]},
        "history_events": {"available": history.available_count, "used": used["history_events"]},
    }
    statistics = {
        "official_sources": used["official_documents"], "communications_used": used["communications"],
        "notes_used": used["notes"], "history_events_used": used["history_events"],
        "unavailable_sources": len(selection.selection.unavailable_sources),
        "total_selected_characters": selection.selection.statistics.selected_character_count,
        "input_truncated": input_truncated,
    }
    return statistics, source_summary


class OpportunityKnowledgeBriefCompiler:
    def __init__(
        self, db: Session, *, current_state_assembler=None, official_collector=None,
        note_collector=None, communication_collector=None, history_collector=None,
        conflict_detector=None, evidence_selector=None, manifest_builder=None,
        manifest_hasher=None, model_client=None, validator=None,
        clock: Callable[[], datetime] = _utcnow,
    ):
        self.db = db
        self.current_state_assembler = current_state_assembler or CurrentStateAssembler(db)
        self.official_collector = official_collector or OfficialEvidenceCollector(db)
        self.note_collector = note_collector or NoteEvidenceCollector(db)
        self.communication_collector = communication_collector or CommunicationEvidenceCollector(db)
        self.history_collector = history_collector or HistoricalEvidenceCollector(db)
        self.conflict_detector = conflict_detector or ConflictDetector()
        self.evidence_selector = evidence_selector or EvidenceSelector(maximum_total_characters=config.GUTS_MAX_TOTAL_INPUT_CHARS)
        self.manifest_builder = manifest_builder or ManifestBuilder()
        self.manifest_hasher = manifest_hasher or ManifestHasher()
        self.model_client = model_client
        self.validator = validator
        self.clock = clock

    def generate(self, *, generation: OpportunityKnowledgeBriefGeneration, access_context: GUTSAccessContext, authorization_ms: int = 0) -> OpportunityKnowledgeBriefGeneration:
        total_started = perf_counter()
        timings: dict[str, Any] = {"authorization_ms": authorization_ms}
        stage = "lifecycle"
        persistence_started: float | None = None
        try:
            if generation.status != GenerationStatus.PENDING:
                raise GUTSCompilerError("unexpected_error", "The generation attempt is not pending.", stage="lifecycle")
            mark_generation_running(self.db, generation, started_at=self.clock())
            snapshot_started = self.clock()
            update_active_generation_metadata(self.db, generation, metadata={"source_snapshot_started_at": snapshot_started, **timings})

            stage = "current_state"; started = perf_counter()
            state = self.current_state_assembler.build(
                opportunity=access_context.opportunity, organization_id=access_context.organization_id,
                workspace_id=access_context.workspace_id,
            )
            timings["current_state_ms"] = _elapsed_ms(started)
            if not has_minimum_evidence(state):
                raise GUTSCompilerError("insufficient_evidence", "This opportunity does not yet contain enough information to generate a briefing.", stage="current_state")

            collector_args = {"opportunity_id": state.opportunity_id, "organization_id": state.organization_id}
            stage = "official_evidence"; started = perf_counter()
            official = self.official_collector.collect(**collector_args, workspace_id=state.workspace_id)
            timings["official_evidence_ms"] = _elapsed_ms(started)
            stage = "notes"; started = perf_counter()
            notes = self.note_collector.collect(**collector_args)
            timings["notes_ms"] = _elapsed_ms(started)
            stage = "communication"; started = perf_counter()
            communications = self.communication_collector.collect(**collector_args, workspace_id=state.workspace_id)
            timings["communication_ms"] = _elapsed_ms(started)
            stage = "history"; started = perf_counter()
            history = self.history_collector.collect(**collector_args)
            timings["history_ms"] = _elapsed_ms(started)
            snapshot_completed = self.clock()

            stage = "manifest"; started = perf_counter()
            all_evidence = (*official.evidence, *notes.evidence, *communications.evidence, *history.evidence)
            conflicts = self.conflict_detector.detect(current_state=state, evidence=all_evidence)
            selection = self.evidence_selector.select(
                current_state=state, official=official, notes=notes,
                communications=communications, historical=history, known_conflicts=conflicts,
            )
            constraints = GenerationConstraints(
                max_total_input_characters=config.GUTS_MAX_TOTAL_INPUT_CHARS,
                max_output_tokens=config.GUTS_MAX_OUTPUT_TOKENS,
                timeout_seconds=config.GUTS_TIMEOUT_SECONDS, max_retries=config.GUTS_MAX_RETRIES,
            )
            manifest = self.manifest_builder.build(
                manifest_version=config.GUTS_MANIFEST_VERSION, current_state=state, selection=selection,
                constraints=constraints, snapshot_started_at=snapshot_started,
                snapshot_completed_at=snapshot_completed,
            )
            manifest_hash = self.manifest_hasher.hash(manifest)
            timings["manifest_ms"] = _elapsed_ms(started)
            input_characters = len(state.canonical_json()) + selection.selection.statistics.selected_character_count
            input_truncated = selection.input_truncated or state.description_was_truncated
            update_active_generation_metadata(self.db, generation, metadata={
                **timings, "manifest_hash": manifest_hash,
                "source_snapshot_completed_at": snapshot_completed,
                "latest_source_at": selection.latest_source_at,
                "input_character_count": input_characters,
                "estimated_input_tokens": (input_characters + 3) // 4,
                "degraded_source_count": len(selection.selection.unavailable_sources),
                "input_truncated": input_truncated,
            })

            stage = "model"; started = perf_counter()
            model_result = generate_validated_briefing(manifest, client=self.model_client, validator=self.validator)
            measured_model_ms = _elapsed_ms(started)
            timings["model_ms"] = round(model_result.model_ms)
            timings["validation_ms"] = max(0, measured_model_ms - timings["model_ms"])

            stage = "persistence"; persistence_started = perf_counter()
            statistics, source_summary = _summaries(
                selection, official, notes, communications, history,
                input_truncated=input_truncated,
            )
            communication_coverage = _communication_coverage(selection, model_result.output)
            statistics.update(communication_coverage)
            sources = [*_current_state_sources(state), *(_evidence_source_row(source) for source in selection.selection.sources)]
            completed = save_generation_success(
                self.db, generation, output_json=model_result.output.serializable_dict(),
                current_state_snapshot_json=self.current_state_assembler.compact_snapshot(state),
                sources=sources, statements=_statement_rows(model_result.output),
                reproducibility_status=selection.reproducibility_status, completed_at=self.clock(),
                metadata={
                    **timings, "manifest_hash": manifest_hash,
                    "source_snapshot_started_at": snapshot_started,
                    "source_snapshot_completed_at": snapshot_completed,
                    "latest_source_at": selection.latest_source_at,
                    "source_summary_json": source_summary,
                    "warning_metadata_json": _warnings(
                        selection, input_truncated=input_truncated,
                        communication_coverage=communication_coverage,
                    ),
                    "statistics_json": statistics, "input_character_count": input_characters,
                    "estimated_input_tokens": (input_characters + 3) // 4,
                    "input_tokens": model_result.input_tokens, "output_tokens": model_result.output_tokens,
                    "total_tokens": model_result.total_tokens,
                    "validation_retry_count": model_result.validation_retry_count,
                    "degraded_source_count": len(selection.selection.unavailable_sources),
                    "input_truncated": input_truncated,
                    "provider": model_result.provider, "model": model_result.model,
                }, persistence_started_monotonic=persistence_started, total_started_monotonic=total_started,
            )
            logger.info(
                "guts_generation_succeeded generation_id=%s opportunity_id=%s organization_id=%s total_ms=%s sources=%s statements=%s provider=%s model=%s retries=%s",
                completed.id, completed.opportunity_id, completed.organization_id, completed.total_ms,
                len(sources), len(completed.statements), completed.provider, completed.model,
                completed.validation_retry_count,
            )
            return completed
        except Exception as exc:
            safe = self._safe_error(exc, stage)
            if isinstance(exc, GUTSModelError):
                timings["model_ms"] = round(exc.model_ms)
                for key in ("input_tokens", "output_tokens", "total_tokens"):
                    if exc.usage.get(key) is not None:
                        timings[key] = exc.usage[key]
            if stage == "persistence" and persistence_started is not None:
                timings["persistence_ms"] = _elapsed_ms(persistence_started)
            failure_metadata = {**timings, "total_ms": _elapsed_ms(total_started)}
            try:
                self.db.rollback()
                self.db.refresh(generation)
                if generation.status in {GenerationStatus.PENDING, GenerationStatus.RUNNING}:
                    mark_generation_failed(
                        self.db, generation, failure_category=safe.safe_category,
                        failure_stage=safe.stage, safe_error_message=safe.safe_message,
                        completed_at=self.clock(), metadata=failure_metadata,
                    )
            except Exception as persistence_exc:
                self.db.rollback()
                logger.error(
                    "guts_generation_failure_persistence_failed generation_id=%s opportunity_id=%s organization_id=%s stage=%s category=persistence_failed exception_type=%s",
                    generation.id, generation.opportunity_id, generation.organization_id, safe.stage,
                    type(persistence_exc).__name__,
                )
                raise GUTSCompilerError("persistence_failed", "The briefing failure could not be recorded safely.", stage="persistence") from None
            logger.warning(
                "guts_generation_failed generation_id=%s opportunity_id=%s organization_id=%s stage=%s category=%s total_ms=%s exception_type=%s",
                generation.id, generation.opportunity_id, generation.organization_id, safe.stage,
                safe.safe_category, failure_metadata["total_ms"], type(exc).__name__,
            )
            raise safe from None

    @staticmethod
    def _safe_error(exc: Exception, stage: str) -> GUTSCompilerError:
        if isinstance(exc, GUTSCompilerError):
            return exc
        if isinstance(exc, GUTSModelError):
            return GUTSCompilerError(
                exc.safe_category, exc.safe_message, stage=exc.stage,
                retryable=exc.retryable, validation_debug=exc.validation_debug,
                provider_debug=exc.provider_debug,
                schema_debug=exc.schema_debug,
            )
        if isinstance(exc, ManifestValidationError):
            return GUTSCompilerError("manifest_validation_failed", "The briefing evidence manifest was invalid.", stage="manifest")
        if isinstance(exc, KnowledgeBriefPersistenceError) or stage == "persistence":
            return GUTSCompilerError("persistence_failed", "The briefing could not be saved.", stage="persistence")
        if stage in {"official_evidence", "notes", "communication", "history"}:
            return GUTSCompilerError("source_collection_failed", "The briefing evidence could not be collected.", stage=stage)
        if stage == "manifest":
            return GUTSCompilerError("manifest_build_failed", "The briefing evidence manifest could not be built.", stage="manifest")
        return GUTSCompilerError("unexpected_error", "The briefing could not be generated.", stage=stage)
