from __future__ import annotations

import logging
import secrets
from dataclasses import asdict, dataclass
from time import perf_counter

from sqlalchemy.orm import Session

from ... import config
from ...models import OpportunityIntakeDraft, OpportunitySourceMaterial
from .document_extraction import IntakeExtractionError
from .document_parsing import IntakeDocumentError, parse_intake_document, validate_intake_document
from .drafts import create_draft, store_source_material, update_draft
from .email_extraction import IntakeEmailExtractor, OpenAIIntakeEmailExtractor
from .email_parsing import IntakeEmailError, ParsedIntakeEmail, parse_intake_email, validate_intake_email
from .storage import SourceMaterialStorage, SourceMaterialStorageError, cleanup_uploaded_objects


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailUploadResult:
    draft: OpportunityIntakeDraft
    email_material: OpportunitySourceMaterial
    attachment_materials: tuple[OpportunitySourceMaterial, ...]
    warnings: tuple[str, ...]
    timings_ms: dict[str, float]


def _candidate_values(candidate) -> dict:
    values = asdict(candidate)
    if candidate.response_deadline:
        values["response_deadline"] = candidate.response_deadline.isoformat()
    return {key: value for key, value in values.items() if value is not None}


def _deterministic_candidate(parsed: ParsedIntakeEmail) -> dict[str, str]:
    return {"title": parsed.subject} if parsed.subject else {}


def assemble_email_extraction_input(
    parsed: ParsedIntakeEmail,
    attachment_texts: list[tuple[str, str]],
    *,
    max_chars: int | None = None,
) -> str:
    limit = config.INTAKE_EMAIL_MAX_EXTRACTION_CHARS if max_chars is None else max_chars
    sections: list[str] = []
    if parsed.subject:
        sections.append(f"EMAIL SUBJECT\n{parsed.subject}")
    if parsed.body_text:
        sections.append(f"EMAIL BODY\n{parsed.body_text}")
    for index, (filename, text) in enumerate(attachment_texts, start=1):
        sections.append(f"ATTACHMENT {index} — PRIMARY AUTHORITY ({filename})\n{text}")
    assembled = "\n\n".join(sections)
    if limit <= 0:
        return ""
    return assembled if len(assembled) <= limit else assembled[: limit - 1].rstrip() + "…"


def _commit_or_cleanup(db: Session, storage: SourceMaterialStorage, keys: list[str]) -> None:
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        if cleanup_uploaded_objects(storage, keys):
            raise SourceMaterialStorageError(
                "Email metadata persistence failed and uploaded-object cleanup could not be confirmed"
            ) from exc
        raise


def process_email_file(
    db: Session,
    storage: SourceMaterialStorage,
    *,
    organization_id: int,
    workspace_id: int,
    user_id: int,
    filename: str | None,
    mime_type: str | None,
    content: bytes,
    extractor: IntakeEmailExtractor | None = None,
) -> EmailUploadResult:
    total_started = perf_counter()
    validate_intake_email(filename=filename, mime_type=mime_type, content=content)
    draft = create_draft(
        db,
        organization_id=organization_id,
        workspace_id=workspace_id,
        user_id=user_id,
        intake_method="email",
        add_to_shortlist=True,
        publish_idempotency_key=secrets.token_urlsafe(24),
    )
    draft.status = "EXTRACTING"
    stored_keys: list[str] = []
    storage_started = perf_counter()
    email_material = store_source_material(
        db,
        storage,
        draft_id=draft.id,
        organization_id=organization_id,
        workspace_id=workspace_id,
        user_id=user_id,
        material_type="email",
        original_filename=filename,
        content=content,
        mime_type=mime_type,
        provider="email_file",
    )
    stored_keys.append(email_material.storage_key)
    timings = {"storage_ms": round((perf_counter() - storage_started) * 1000, 2)}
    warnings: list[str] = []
    attachment_materials: list[OpportunitySourceMaterial] = []
    extraction_metadata: dict = {
        "source_material_id": email_material.id,
        "openai_request_count": 0,
    }
    try:
        parse_started = perf_counter()
        parsed = parse_intake_email(
            filename=email_material.original_filename,
            mime_type=email_material.mime_type,
            content=content,
        )
        timings["parsing_ms"] = round((perf_counter() - parse_started) * 1000, 2)
        warnings.extend(parsed.warnings)
        email_material.internet_message_id = parsed.internet_message_id
        email_material.provider = parsed.provider or "email_file"
        email_material.provider_message_id = parsed.provider_message_id
        email_material.provider_metadata_json = {
            "subject": parsed.subject,
            "sender": parsed.sender,
            "to_recipients": list(parsed.to_recipients),
            "cc_recipients": list(parsed.cc_recipients),
            "sent_at": parsed.sent_at,
            "body_source": parsed.body_source,
            "body_characters": len(parsed.body_text),
            "attachment_count": len(parsed.attachments),
            "skipped_attachments": list(parsed.skipped_attachments),
        }
        email_material.parsed_metadata_json = {
            "parser_type": "python_email",
            "body_source": parsed.body_source,
            "body_characters": len(parsed.body_text),
            "warnings": list(parsed.warnings),
        }
        email_material.parse_status = "COMPLETE"
        update_draft(
            db,
            draft_id=draft.id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            user_id=user_id,
            candidate=_deterministic_candidate(parsed),
        )

        attachment_started = perf_counter()
        attachment_texts: list[tuple[str, str]] = []
        for attachment in parsed.attachments:
            if perf_counter() - attachment_started > config.INTAKE_EMAIL_MAX_PARSING_SECONDS:
                warnings.append("Attachment parsing stopped after reaching the configured time limit.")
                break
            try:
                validate_intake_document(
                    filename=attachment.filename,
                    mime_type=attachment.mime_type,
                    content=attachment.content,
                    max_bytes=config.INTAKE_EMAIL_MAX_ATTACHMENT_BYTES,
                )
            except IntakeDocumentError as exc:
                warnings.append(f"{attachment.filename}: {exc}")
                continue
            material = store_source_material(
                db,
                storage,
                draft_id=draft.id,
                organization_id=organization_id,
                workspace_id=workspace_id,
                user_id=user_id,
                material_type="email_attachment",
                original_filename=attachment.filename,
                content=attachment.content,
                mime_type=attachment.mime_type,
                parent_material_id=email_material.id,
                max_file_bytes=config.INTAKE_EMAIL_MAX_ATTACHMENT_BYTES,
            )
            stored_keys.append(material.storage_key)
            attachment_materials.append(material)
            try:
                document = parse_intake_document(
                    filename=material.original_filename,
                    mime_type=material.mime_type,
                    content=attachment.content,
                )
                material.parsed_metadata_json = {
                    "parser_type": document.parser_type,
                    "page_count": document.page_count,
                    "total_characters": document.total_characters,
                    "warnings": list(document.warnings),
                    **(document.metadata or {}),
                }
                warnings.extend(f"{attachment.filename}: {warning}" for warning in document.warnings)
                if document.extracted_text:
                    material.parse_status = "COMPLETE"
                    attachment_texts.append((attachment.filename, document.extracted_text))
                else:
                    material.parse_status = "FAILED"
                    material.parse_error_code = "no_readable_text"
            except IntakeDocumentError as exc:
                material.parse_status = "FAILED"
                material.parse_error_code = exc.code
                warnings.append(f"{attachment.filename}: {exc}")
        timings["attachment_parsing_ms"] = round((perf_counter() - attachment_started) * 1000, 2)

        extraction_input = assemble_email_extraction_input(parsed, attachment_texts)
        if extraction_input:
            extraction_started = perf_counter()
            extraction_metadata["openai_request_count"] = 1
            try:
                extracted = (extractor or OpenAIIntakeEmailExtractor()).extract(extraction_input)
                timings["extraction_ms"] = round((perf_counter() - extraction_started) * 1000, 2)
                warnings.extend(extracted.result.warnings)
                extraction_metadata.update({
                    "status": "complete",
                    "provider": extracted.result.provider,
                    "model": extracted.result.model,
                    "confidence": extracted.result.confidence,
                    "evidence": extracted.result.evidence,
                    "source_attribution": extracted.source_attribution,
                    "warnings": list(extracted.result.warnings),
                    "usage": extracted.result.usage,
                    "input_characters": len(extraction_input),
                    "attachment_text_count": len(attachment_texts),
                })
                update_draft(
                    db,
                    draft_id=draft.id,
                    organization_id=organization_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    candidate=_candidate_values(extracted.result.candidate),
                )
            except IntakeExtractionError as exc:
                timings["extraction_ms"] = round((perf_counter() - extraction_started) * 1000, 2)
                warnings.append(str(exc))
                extraction_metadata.update({"status": "failed", "error_code": exc.code})
        else:
            extraction_metadata.update({"status": "skipped", "reason": "no_readable_text"})
        draft.status = "READY"
    except IntakeEmailError as exc:
        timings["parsing_ms"] = round((perf_counter() - parse_started) * 1000, 2)
        email_material.parse_status = "FAILED"
        email_material.parse_error_code = exc.code
        warnings.append(str(exc))
        extraction_metadata.update({"status": "skipped", "reason": exc.code})
        draft.status = "READY"
    except Exception as exc:
        db.rollback()
        if cleanup_uploaded_objects(storage, stored_keys):
            raise SourceMaterialStorageError(
                "Email intake failed and uploaded-object cleanup could not be confirmed"
            ) from exc
        raise

    timings.setdefault("attachment_parsing_ms", 0.0)
    timings.setdefault("extraction_ms", 0.0)
    timings["total_ms"] = round((perf_counter() - total_started) * 1000, 2)
    draft.extraction_metadata_json = {
        **extraction_metadata,
        "warnings": warnings,
        "timings_ms": timings,
    }
    _commit_or_cleanup(db, storage, stored_keys)
    logger.info(
        "opportunity_intake_email_timing organization_id=%s workspace_id=%s draft_id=%s email_material_id=%s attachment_count=%s storage_ms=%s parsing_ms=%s attachment_parsing_ms=%s extraction_ms=%s total_ms=%s openai_request_count=%s",
        organization_id,
        workspace_id,
        draft.id,
        email_material.id,
        len(attachment_materials),
        timings.get("storage_ms", 0.0),
        timings.get("parsing_ms", 0.0),
        timings.get("attachment_parsing_ms", 0.0),
        timings.get("extraction_ms", 0.0),
        timings.get("total_ms", 0.0),
        extraction_metadata.get("openai_request_count", 0),
    )
    return EmailUploadResult(
        draft=draft,
        email_material=email_material,
        attachment_materials=tuple(attachment_materials),
        warnings=tuple(warnings),
        timings_ms=timings,
    )
