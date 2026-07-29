from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy.orm import Session

from ... import config
from ...models import (
    Opportunity,
    OpportunityIntakeDraft,
    OpportunitySourceMaterial,
    OrganizationMembership,
    Workspace,
)
from .contracts import DEFAULT_ADD_TO_SHORTLIST, IntakeCandidate, IntakeMethod
from .normalization import normalize_candidate
from .references import format_internal_reference
from .storage import (
    SourceMaterialStorage,
    SourceMaterialStorageError,
    cleanup_uploaded_objects,
    generate_storage_key,
    sanitize_original_filename,
)


logger = logging.getLogger(__name__)


DRAFT_STATUSES = frozenset({"DRAFT", "EXTRACTING", "READY", "PUBLISHED", "FAILED"})
DEFAULT_DRAFT_LIFETIME = timedelta(days=7)


class DraftAccessError(LookupError):
    pass


class SourceMaterialValidationError(ValueError):
    pass


@dataclass(frozen=True)
class DraftCleanupResult:
    drafts_expired: int = 0
    materials_deleted: int = 0
    materials_preserved: int = 0
    storage_failures: int = 0


@dataclass(frozen=True)
class SourceMaterialReconciliationReport:
    metadata_objects: int = 0
    storage_objects: int = 0
    missing_object_material_ids: tuple[int, ...] = ()
    unreferenced_storage_keys: tuple[str, ...] = ()
    expired_unpublished_material_ids: tuple[int, ...] = ()
    errors: tuple[str, ...] = ()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _comparable_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _candidate_json(candidate: IntakeCandidate | Mapping[str, Any] | None) -> dict[str, Any]:
    if candidate is None:
        return {}
    normalized = normalize_candidate(candidate)
    values = asdict(normalized)
    if normalized.response_deadline:
        values["response_deadline"] = normalized.response_deadline.isoformat()
    return {key: value for key, value in values.items() if value is not None}


def _require_membership(
    db: Session,
    *,
    organization_id: int,
    workspace_id: int,
    user_id: int,
) -> Workspace:
    workspace = db.query(Workspace).filter(
        Workspace.id == workspace_id,
        Workspace.organization_id == organization_id,
    ).one_or_none()
    membership = db.query(OrganizationMembership.id).filter(
        OrganizationMembership.organization_id == organization_id,
        OrganizationMembership.user_id == user_id,
    ).first()
    if workspace is None or membership is None:
        raise DraftAccessError("Opportunity intake draft is not available")
    return workspace


def create_draft(
    db: Session,
    *,
    organization_id: int,
    workspace_id: int,
    user_id: int,
    intake_method: IntakeMethod | str,
    candidate: IntakeCandidate | Mapping[str, Any] | None = None,
    extraction_metadata: Mapping[str, Any] | None = None,
    validation_errors: list[Mapping[str, Any]] | None = None,
    add_to_shortlist: bool = DEFAULT_ADD_TO_SHORTLIST,
    publish_idempotency_key: str | None = None,
    expires_at: datetime | None = None,
    now: datetime | None = None,
) -> OpportunityIntakeDraft:
    _require_membership(
        db,
        organization_id=organization_id,
        workspace_id=workspace_id,
        user_id=user_id,
    )
    method = IntakeMethod(intake_method)
    created_at = now or _utc_now()
    draft = OpportunityIntakeDraft(
        organization_id=organization_id,
        workspace_id=workspace_id,
        created_by_user_id=user_id,
        intake_method=method.value,
        status="DRAFT",
        candidate_fields_json=_candidate_json(candidate),
        extraction_metadata_json=dict(extraction_metadata) if extraction_metadata else None,
        validation_errors_json=list(validation_errors) if validation_errors else None,
        add_to_shortlist=bool(add_to_shortlist),
        publish_idempotency_key=(publish_idempotency_key or "").strip() or None,
        expires_at=expires_at or created_at + DEFAULT_DRAFT_LIFETIME,
        created_at=created_at,
        updated_at=created_at,
    )
    db.add(draft)
    db.flush()
    draft.internal_reference = format_internal_reference(draft.id, year=created_at.year)
    db.flush()
    return draft


def get_draft(
    db: Session,
    *,
    draft_id: int,
    organization_id: int,
    workspace_id: int,
    user_id: int,
    require_creator: bool = True,
) -> OpportunityIntakeDraft:
    _require_membership(
        db,
        organization_id=organization_id,
        workspace_id=workspace_id,
        user_id=user_id,
    )
    query = db.query(OpportunityIntakeDraft).filter(
        OpportunityIntakeDraft.id == draft_id,
        OpportunityIntakeDraft.organization_id == organization_id,
        OpportunityIntakeDraft.workspace_id == workspace_id,
    )
    if require_creator:
        query = query.filter(OpportunityIntakeDraft.created_by_user_id == user_id)
    draft = query.one_or_none()
    if draft is None:
        raise DraftAccessError("Opportunity intake draft is not available")
    return draft


def update_draft(
    db: Session,
    *,
    draft_id: int,
    organization_id: int,
    workspace_id: int,
    user_id: int,
    candidate: IntakeCandidate | Mapping[str, Any] | None = None,
    status: str | None = None,
    extraction_metadata: Mapping[str, Any] | None = None,
    validation_errors: list[Mapping[str, Any]] | None = None,
    add_to_shortlist: bool | None = None,
) -> OpportunityIntakeDraft:
    draft = get_draft(
        db,
        draft_id=draft_id,
        organization_id=organization_id,
        workspace_id=workspace_id,
        user_id=user_id,
    )
    if status is not None:
        normalized_status = str(status).strip().upper()
        if normalized_status not in DRAFT_STATUSES:
            raise ValueError("Invalid opportunity intake draft status")
        draft.status = normalized_status
    if candidate is not None:
        draft.candidate_fields_json = _candidate_json(candidate)
    if extraction_metadata is not None:
        draft.extraction_metadata_json = dict(extraction_metadata)
    if validation_errors is not None:
        draft.validation_errors_json = list(validation_errors)
    if add_to_shortlist is not None:
        draft.add_to_shortlist = bool(add_to_shortlist)
    db.flush()
    return draft


def store_source_material(
    db: Session,
    storage: SourceMaterialStorage,
    *,
    draft_id: int,
    organization_id: int,
    workspace_id: int,
    user_id: int,
    material_type: str,
    original_filename: str | None,
    content: bytes,
    mime_type: str | None = None,
    parent_material_id: int | None = None,
    provider: str | None = None,
    provider_metadata: Mapping[str, Any] | None = None,
    provider_message_id: str | None = None,
    internet_message_id: str | None = None,
    max_file_bytes: int | None = None,
) -> OpportunitySourceMaterial:
    draft = get_draft(
        db,
        draft_id=draft_id,
        organization_id=organization_id,
        workspace_id=workspace_id,
        user_id=user_id,
    )
    if not isinstance(content, bytes):
        raise SourceMaterialValidationError("Source material content must be bytes")
    if not content:
        raise SourceMaterialValidationError("Source material cannot be empty")
    size_limit = config.SOURCE_MATERIAL_MAX_BYTES if max_file_bytes is None else max_file_bytes
    if size_limit <= 0 or len(content) > size_limit:
        raise SourceMaterialValidationError("Source material exceeds the configured file-size limit")
    material_kind = str(material_type or "").strip().lower()
    if not material_kind:
        raise SourceMaterialValidationError("Source material type is required")
    parent = None
    if parent_material_id is not None:
        parent = db.query(OpportunitySourceMaterial).filter(
            OpportunitySourceMaterial.id == parent_material_id,
            OpportunitySourceMaterial.organization_id == organization_id,
            OpportunitySourceMaterial.workspace_id == workspace_id,
            OpportunitySourceMaterial.intake_draft_id == draft.id,
        ).one_or_none()
        if parent is None:
            raise DraftAccessError("Parent source material is not available")

    storage_key = generate_storage_key(
        organization_id=organization_id,
        workspace_id=workspace_id,
        draft_id=draft.id,
    )
    storage.put(storage_key, content)
    material = OpportunitySourceMaterial(
        organization_id=organization_id,
        workspace_id=workspace_id,
        intake_draft_id=draft.id,
        parent_material_id=parent.id if parent else None,
        material_type=material_kind,
        original_filename=sanitize_original_filename(original_filename),
        mime_type=(mime_type or "").strip().lower() or None,
        byte_size=len(content),
        sha256_digest=hashlib.sha256(content).hexdigest(),
        storage_key=storage_key,
        provider=(provider or "").strip() or None,
        provider_metadata_json=dict(provider_metadata) if provider_metadata else None,
        provider_message_id=(provider_message_id or "").strip() or None,
        internet_message_id=(internet_message_id or "").strip() or None,
        parse_status="PENDING",
    )
    db.add(material)
    try:
        db.flush()
    except Exception as exc:
        if cleanup_uploaded_objects(storage, [storage_key]):
            raise SourceMaterialStorageError(
                "Source-material metadata persistence failed and uploaded-object cleanup could not be confirmed"
            ) from exc
        raise
    return material


def preserve_materials_for_opportunity(
    db: Session,
    *,
    draft: OpportunityIntakeDraft,
    opportunity: Opportunity,
) -> int:
    if opportunity.organization_id != draft.organization_id:
        raise DraftAccessError("Opportunity is outside the intake draft organization")
    materials = db.query(OpportunitySourceMaterial).filter(
        OpportunitySourceMaterial.organization_id == draft.organization_id,
        OpportunitySourceMaterial.workspace_id == draft.workspace_id,
        OpportunitySourceMaterial.intake_draft_id == draft.id,
    ).all()
    for material in materials:
        material.opportunity_id = opportunity.id
    db.flush()
    return len(materials)


def expire_abandoned_drafts(
    db: Session,
    storage: SourceMaterialStorage,
    *,
    now: datetime | None = None,
    organization_id: int | None = None,
    workspace_id: int | None = None,
) -> DraftCleanupResult:
    cutoff = now or _utc_now()
    query = db.query(OpportunityIntakeDraft).filter(
        OpportunityIntakeDraft.status != "PUBLISHED",
        OpportunityIntakeDraft.expires_at <= cutoff,
    )
    if organization_id is not None:
        query = query.filter(OpportunityIntakeDraft.organization_id == organization_id)
    if workspace_id is not None:
        query = query.filter(OpportunityIntakeDraft.workspace_id == workspace_id)

    expired = deleted = preserved = failures = 0
    for draft in query.all():
        materials = list(draft.source_materials)
        try:
            for material in materials:
                if material.opportunity_id is not None:
                    continue
                storage.delete(material.storage_key)
        except Exception:
            failures += 1
            logger.error(
                "source_material_cleanup_failed draft_id=%s organization_id=%s workspace_id=%s material_count=%s",
                draft.id,
                draft.organization_id,
                draft.workspace_id,
                len(materials),
            )
            continue
        for material in materials:
            if material.opportunity_id is not None:
                material.intake_draft_id = None
                preserved += 1
            else:
                db.delete(material)
                deleted += 1
        db.flush()
        db.delete(draft)
        db.flush()
        expired += 1
        logger.info(
            "source_material_cleanup_succeeded draft_id=%s organization_id=%s workspace_id=%s materials_deleted=%s materials_preserved=%s",
            draft.id,
            draft.organization_id,
            draft.workspace_id,
            sum(1 for material in materials if material.opportunity_id is None),
            sum(1 for material in materials if material.opportunity_id is not None),
        )
    return DraftCleanupResult(expired, deleted, preserved, failures)


def reconcile_source_materials(
    db: Session,
    storage: SourceMaterialStorage,
    *,
    organization_id: int | None = None,
    workspace_id: int | None = None,
    now: datetime | None = None,
    detail_limit: int = 100,
) -> SourceMaterialReconciliationReport:
    cutoff = now or _utc_now()
    query = db.query(OpportunitySourceMaterial)
    if organization_id is not None:
        query = query.filter(OpportunitySourceMaterial.organization_id == organization_id)
    if workspace_id is not None:
        query = query.filter(OpportunitySourceMaterial.workspace_id == workspace_id)
    materials = query.all()
    metadata_keys = {material.storage_key for material in materials}
    missing: list[int] = []
    errors: list[str] = []
    for material in materials:
        try:
            if not storage.exists(material.storage_key):
                missing.append(material.id)
        except Exception:
            errors.append("storage_existence_check_failed")

    prefix = ""
    if organization_id is not None:
        prefix = f"org-{organization_id}/"
        if workspace_id is not None:
            prefix += f"workspace-{workspace_id}/"
    try:
        storage_keys = set(storage.list_keys(prefix))
    except Exception:
        storage_keys = set()
        errors.append("storage_listing_failed")
    unreferenced = sorted(storage_keys - metadata_keys)
    expired = [
        material.id
        for material in materials
        if material.opportunity_id is None
        and material.intake_draft is not None
        and material.intake_draft.status != "PUBLISHED"
        and _comparable_utc(material.intake_draft.expires_at) <= _comparable_utc(cutoff)
    ]
    limit = max(0, int(detail_limit))
    report = SourceMaterialReconciliationReport(
        metadata_objects=len(materials),
        storage_objects=len(storage_keys),
        missing_object_material_ids=tuple(missing[:limit]),
        unreferenced_storage_keys=tuple(unreferenced[:limit]),
        expired_unpublished_material_ids=tuple(expired[:limit]),
        errors=tuple(errors[:limit]),
    )
    logger.info(
        "source_material_reconciliation metadata_objects=%s storage_objects=%s missing_objects=%s unreferenced_objects=%s expired_unpublished=%s errors=%s",
        report.metadata_objects,
        report.storage_objects,
        len(missing),
        len(unreferenced),
        len(expired),
        len(errors),
    )
    return report
