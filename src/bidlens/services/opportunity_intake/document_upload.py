from __future__ import annotations

import logging
import secrets
from dataclasses import asdict, dataclass
from time import perf_counter

from sqlalchemy.orm import Session

from ...models import OpportunityIntakeDraft, OpportunitySourceMaterial
from .document_extraction import (
    IntakeDocumentExtractor,
    IntakeExtractionError,
    OpenAIIntakeDocumentExtractor,
)
from .document_parsing import IntakeDocumentError, parse_intake_document, validate_intake_document
from .drafts import create_draft, store_source_material, update_draft
from .storage import SourceMaterialStorage
from .storage import SourceMaterialStorageError, cleanup_uploaded_objects


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DocumentUploadResult:
    draft: OpportunityIntakeDraft
    material: OpportunitySourceMaterial
    warnings: tuple[str, ...]
    timings_ms: dict[str, float]


def _candidate_values(candidate) -> dict:
    values = asdict(candidate)
    if candidate.response_deadline:
        values["response_deadline"] = candidate.response_deadline.isoformat()
    return {key: value for key, value in values.items() if value is not None}


def process_rfp_document(
    db: Session,
    storage: SourceMaterialStorage,
    *,
    organization_id: int,
    workspace_id: int,
    user_id: int,
    filename: str | None,
    mime_type: str | None,
    content: bytes,
    extractor: IntakeDocumentExtractor | None = None,
) -> DocumentUploadResult:
    total_started = perf_counter()
    validate_intake_document(filename=filename, mime_type=mime_type, content=content)
    draft = create_draft(
        db,
        organization_id=organization_id,
        workspace_id=workspace_id,
        user_id=user_id,
        intake_method="document",
        add_to_shortlist=True,
        publish_idempotency_key=secrets.token_urlsafe(24),
    )
    draft.status = "EXTRACTING"
    storage_started = perf_counter()
    material = store_source_material(
        db,
        storage,
        draft_id=draft.id,
        organization_id=organization_id,
        workspace_id=workspace_id,
        user_id=user_id,
        material_type="rfp_document",
        original_filename=filename,
        content=content,
        mime_type=mime_type,
    )
    timings = {"storage_ms": round((perf_counter() - storage_started) * 1000, 2)}
    warnings: list[str] = []
    extraction_metadata: dict = {"source_material_id": material.id, "openai_request_count": 0}
    try:
        parse_started = perf_counter()
        parsed = parse_intake_document(
            filename=material.original_filename,
            mime_type=material.mime_type,
            content=content,
        )
        timings["parsing_ms"] = round((perf_counter() - parse_started) * 1000, 2)
        warnings.extend(parsed.warnings)
        material.parsed_metadata_json = {
            "parser_type": parsed.parser_type,
            "page_count": parsed.page_count,
            "total_characters": parsed.total_characters,
            "warnings": list(parsed.warnings),
            **(parsed.metadata or {}),
        }
        if not parsed.extracted_text:
            material.parse_status = "FAILED"
            material.parse_error_code = "no_readable_text"
            extraction_metadata.update({"status": "skipped", "reason": "no_readable_text"})
        else:
            material.parse_status = "COMPLETE"
            extraction_started = perf_counter()
            extraction_metadata["openai_request_count"] = 1
            try:
                extracted = (extractor or OpenAIIntakeDocumentExtractor()).extract(parsed.extracted_text)
                timings["extraction_ms"] = round((perf_counter() - extraction_started) * 1000, 2)
                warnings.extend(extracted.warnings)
                extraction_metadata.update({
                    "status": "complete",
                    "provider": extracted.provider,
                    "model": extracted.model,
                    "confidence": extracted.confidence,
                    "evidence": extracted.evidence,
                    "warnings": list(extracted.warnings),
                    "usage": extracted.usage,
                })
                update_draft(
                    db,
                    draft_id=draft.id,
                    organization_id=organization_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    candidate=_candidate_values(extracted.candidate),
                    status="READY",
                    extraction_metadata=extraction_metadata,
                )
            except IntakeExtractionError as exc:
                timings["extraction_ms"] = round((perf_counter() - extraction_started) * 1000, 2)
                warnings.append(str(exc))
                extraction_metadata.update({"status": "failed", "error_code": exc.code})
        if draft.status == "EXTRACTING" or draft.status == "DRAFT":
            update_draft(
                db,
                draft_id=draft.id,
                organization_id=organization_id,
                workspace_id=workspace_id,
                user_id=user_id,
                status="READY",
                extraction_metadata={**extraction_metadata, "warnings": warnings},
            )
        elif warnings:
            draft.extraction_metadata_json = {
                **(draft.extraction_metadata_json or extraction_metadata),
                "warnings": warnings,
            }
        timings.setdefault("extraction_ms", 0.0)
        timings["total_ms"] = round((perf_counter() - total_started) * 1000, 2)
        db.commit()
    except IntakeDocumentError as exc:
        material.parse_status = "FAILED"
        material.parse_error_code = exc.code
        warnings.append(str(exc))
        draft.status = "READY"
        draft.extraction_metadata_json = {
            **extraction_metadata,
            "status": "skipped",
            "reason": exc.code,
            "warnings": warnings,
        }
        timings.setdefault("parsing_ms", round((perf_counter() - total_started) * 1000, 2))
        timings["extraction_ms"] = 0.0
        timings["total_ms"] = round((perf_counter() - total_started) * 1000, 2)
        try:
            db.commit()
        except Exception as commit_exc:
            db.rollback()
            if cleanup_uploaded_objects(storage, [material.storage_key]):
                raise SourceMaterialStorageError(
                    "Document metadata persistence failed and uploaded-object cleanup could not be confirmed"
                ) from commit_exc
            raise
    except Exception as exc:
        db.rollback()
        if cleanup_uploaded_objects(storage, [material.storage_key]):
            raise SourceMaterialStorageError(
                "Document intake failed and uploaded-object cleanup could not be confirmed"
            ) from exc
        raise
    logger.info(
        "opportunity_intake_document_timing organization_id=%s workspace_id=%s draft_id=%s material_id=%s storage_ms=%s parsing_ms=%s extraction_ms=%s total_ms=%s openai_request_count=%s",
        organization_id,
        workspace_id,
        draft.id,
        material.id,
        timings.get("storage_ms", 0.0),
        timings.get("parsing_ms", 0.0),
        timings.get("extraction_ms", 0.0),
        timings.get("total_ms", 0.0),
        extraction_metadata.get("openai_request_count", 0),
    )
    return DocumentUploadResult(draft=draft, material=material, warnings=tuple(warnings), timings_ms=timings)
