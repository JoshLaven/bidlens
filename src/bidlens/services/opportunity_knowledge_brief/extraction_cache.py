"""Bounded extracted-text cache for retained opportunity source materials."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import PurePath
from typing import Callable

from sqlalchemy.orm import Session

from ...models import OpportunitySourceMaterial, OpportunitySourceMaterialExtraction
from ..opportunity_intake.document_parsing import (
    IntakeDocumentError,
    ParsedIntakeDocument,
    parse_intake_document,
)
from ..opportunity_intake.storage import (
    SourceMaterialStorage,
    SourceMaterialStorageError,
    configured_source_material_storage,
)
from .constants import ExtractionStatus, FailureCategory


logger = logging.getLogger(__name__)
DEFAULT_PARSER_NAME = "bidlens_intake_document"
DEFAULT_PARSER_VERSION = "1"
TRANSIENT_RETRY_SECONDS = 60
SUPPORTED_SUFFIXES = {".pdf", ".docx"}
DETERMINISTIC_PARSE_CODES = {
    "unsupported_legacy_doc",
    "unsupported_type",
    "empty_file",
    "file_too_large",
    "type_mismatch",
    "invalid_pdf",
    "invalid_docx",
    "no_extractable_text",
}


class SourceMaterialExtractionError(RuntimeError):
    pass


class SourceMaterialExtractionScopeError(SourceMaterialExtractionError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _matching_extraction(
    db: Session,
    *,
    source_material_id: int,
    content_hash: str,
    parser_name: str,
    parser_version: str,
) -> OpportunitySourceMaterialExtraction | None:
    return db.query(OpportunitySourceMaterialExtraction).filter(
        OpportunitySourceMaterialExtraction.source_material_id == source_material_id,
        OpportunitySourceMaterialExtraction.content_hash == content_hash,
        OpportunitySourceMaterialExtraction.parser_name == parser_name,
        OpportunitySourceMaterialExtraction.parser_version == parser_version,
    ).first()


def _persist_failure(
    db: Session,
    extraction: OpportunitySourceMaterialExtraction,
    *,
    category: str,
    code: str,
    message: str,
    deterministic: bool,
    now: datetime,
    warnings: tuple[str, ...] = (),
) -> OpportunitySourceMaterialExtraction:
    extraction.status = ExtractionStatus.FAILED
    extraction.extracted_text = None
    extraction.character_count = None
    extraction.page_count = None
    extraction.failure_category = category
    extraction.safe_error_message = message[:500]
    extraction.extracted_at = now
    extraction.warnings_json = {
        "warnings": list(warnings),
        "failure_code": code,
        "deterministic": deterministic,
    }
    db.commit()
    db.refresh(extraction)
    logger.info(
        "source_material_extraction_failed material_id=%s extraction_id=%s category=%s code=%s deterministic=%s",
        extraction.source_material_id,
        extraction.id,
        category,
        code,
        str(deterministic).lower(),
    )
    return extraction


def get_or_create_extraction(
    db: Session,
    *,
    source_material: OpportunitySourceMaterial,
    organization_id: int,
    workspace_id: int,
    opportunity_id: int | None = None,
    parser_name: str = DEFAULT_PARSER_NAME,
    parser_version: str = DEFAULT_PARSER_VERSION,
    storage: SourceMaterialStorage | None = None,
    parse_document: Callable[..., ParsedIntakeDocument] = parse_intake_document,
    now: datetime | None = None,
    transient_retry_seconds: int = TRANSIENT_RETRY_SECONDS,
) -> OpportunitySourceMaterialExtraction:
    """Return a matching cache entry, parsing once when required.

    Deterministic failures are reused. Transient storage failures reuse the same
    row and may retry after a short cooldown; retries never create duplicate keys.
    """

    if source_material.organization_id != organization_id or source_material.workspace_id != workspace_id:
        raise SourceMaterialExtractionScopeError("Source material is outside the requested workspace scope.")
    if opportunity_id is not None and source_material.opportunity_id != opportunity_id:
        raise SourceMaterialExtractionScopeError("Source material is not associated with the requested opportunity.")
    if not parser_name.strip() or not parser_version.strip():
        raise ValueError("parser_name and parser_version are required")

    current_time = now or _utcnow()
    backend = storage or configured_source_material_storage()
    suffix = PurePath(source_material.original_filename or "").suffix.lower()

    try:
        if not backend.exists(source_material.storage_key):
            raise SourceMaterialStorageError("Source material object was not found")
        content = backend.get(source_material.storage_key)
    except SourceMaterialStorageError:
        content_hash = source_material.sha256_digest
        extraction = _matching_extraction(
            db,
            source_material_id=source_material.id,
            content_hash=content_hash,
            parser_name=parser_name,
            parser_version=parser_version,
        ) or OpportunitySourceMaterialExtraction(
            source_material_id=source_material.id,
            content_hash=content_hash,
            parser_name=parser_name,
            parser_version=parser_version,
        )
        if extraction.id is None:
            db.add(extraction)
            db.flush()
        if extraction.status == ExtractionStatus.FAILED:
            failed_at = _aware(extraction.extracted_at or extraction.updated_at)
            if failed_at and failed_at > current_time - timedelta(seconds=transient_retry_seconds):
                return extraction
        return _persist_failure(
            db,
            extraction,
            category=FailureCategory.SOURCE_RETRIEVAL_FAILED,
            code="storage_unavailable",
            message="The retained source file could not be retrieved.",
            deterministic=False,
            now=current_time,
        )

    content_hash = hashlib.sha256(content).hexdigest()
    extraction = _matching_extraction(
        db,
        source_material_id=source_material.id,
        content_hash=content_hash,
        parser_name=parser_name,
        parser_version=parser_version,
    )
    if extraction and extraction.status == ExtractionStatus.SUCCEEDED:
        return extraction
    if extraction and extraction.status == ExtractionStatus.FAILED:
        metadata = extraction.warnings_json if isinstance(extraction.warnings_json, dict) else {}
        if metadata.get("deterministic", True):
            return extraction
        failed_at = _aware(extraction.extracted_at or extraction.updated_at)
        if failed_at and failed_at > current_time - timedelta(seconds=transient_retry_seconds):
            return extraction
    if extraction is None:
        extraction = OpportunitySourceMaterialExtraction(
            source_material_id=source_material.id,
            content_hash=content_hash,
            parser_name=parser_name,
            parser_version=parser_version,
            status=ExtractionStatus.PENDING,
        )
        db.add(extraction)
        db.flush()
    else:
        extraction.status = ExtractionStatus.PENDING
        extraction.failure_category = None
        extraction.safe_error_message = None

    # Parsing can be comparatively slow. Persist the cache row's pending state
    # first so no database transaction remains open across document parsing.
    expire_on_commit = db.expire_on_commit
    db.expire_on_commit = False
    try:
        db.commit()
    finally:
        db.expire_on_commit = expire_on_commit

    if suffix not in SUPPORTED_SUFFIXES:
        return _persist_failure(
            db,
            extraction,
            category=FailureCategory.SOURCE_PARSE_FAILED,
            code="unsupported_type",
            message="Only retained PDF and DOCX files can be extracted.",
            deterministic=True,
            now=current_time,
        )

    try:
        parsed = parse_document(
            filename=source_material.original_filename,
            mime_type=source_material.mime_type,
            content=content,
        )
    except IntakeDocumentError as exc:
        return _persist_failure(
            db,
            extraction,
            category=FailureCategory.SOURCE_PARSE_FAILED,
            code=exc.code,
            message=str(exc),
            deterministic=exc.code in DETERMINISTIC_PARSE_CODES,
            now=current_time,
        )
    except Exception as exc:
        db.rollback()
        logger.warning(
            "source_material_extraction_unexpected material_id=%s parser=%s parser_version=%s error_type=%s",
            source_material.id,
            parser_name,
            parser_version,
            type(exc).__name__,
        )
        extraction = _matching_extraction(
            db,
            source_material_id=source_material.id,
            content_hash=content_hash,
            parser_name=parser_name,
            parser_version=parser_version,
        )
        if extraction is None:
            extraction = OpportunitySourceMaterialExtraction(
                source_material_id=source_material.id,
                content_hash=content_hash,
                parser_name=parser_name,
                parser_version=parser_version,
            )
            db.add(extraction)
            db.flush()
        return _persist_failure(
            db,
            extraction,
            category=FailureCategory.SOURCE_PARSE_FAILED,
            code="unexpected_parser_error",
            message="BidLens could not extract this retained source file.",
            deterministic=False,
            now=current_time,
        )

    if not parsed.extracted_text.strip():
        return _persist_failure(
            db,
            extraction,
            category=FailureCategory.SOURCE_PARSE_FAILED,
            code="no_extractable_text",
            message="No readable text was found in the retained source file.",
            deterministic=True,
            now=current_time,
            warnings=parsed.warnings,
        )

    extraction.status = ExtractionStatus.SUCCEEDED
    extraction.extracted_text = parsed.extracted_text
    extraction.character_count = parsed.total_characters
    extraction.page_count = parsed.page_count
    extraction.warnings_json = {
        "warnings": list(parsed.warnings),
        "parser_type": parsed.parser_type,
        "metadata": parsed.metadata or {},
    }
    extraction.failure_category = None
    extraction.safe_error_message = None
    extraction.extracted_at = current_time
    db.commit()
    db.refresh(extraction)
    logger.info(
        "source_material_extraction_succeeded material_id=%s extraction_id=%s parser=%s parser_version=%s characters=%s pages=%s",
        source_material.id,
        extraction.id,
        parser_name,
        parser_version,
        extraction.character_count,
        extraction.page_count,
    )
    return extraction
