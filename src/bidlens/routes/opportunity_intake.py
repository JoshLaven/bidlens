from __future__ import annotations

import secrets
import logging
import re
from dataclasses import asdict
from typing import Any, Mapping
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..auth import attach_request_user_context, get_current_user
from ..database import get_db
from .. import config
from ..models import Opportunity, OpportunityIntakeDraft, OpportunitySourceMaterial, OrganizationMembership, Workspace
from ..services.opportunity_intake import (
    DraftAccessError,
    OpportunityDuplicateError,
    OpportunityPublicationConflict,
    OpportunityPublicationValidationError,
    OpportunityPublisher,
    IntakeDocumentError,
    IntakeEmailError,
    SourceMaterialStorageError,
    configured_source_material_storage,
    create_draft,
    find_publication_duplicates,
    get_draft,
    intake_csrf_token,
    process_rfp_document,
    process_email_file,
    update_draft,
    validate_candidate,
    validate_intake_csrf_token,
)


router = APIRouter()
templates = Jinja2Templates(directory="src/bidlens/templates")
logger = logging.getLogger(__name__)


def _require_user(request: Request, db: Session):
    user = get_current_user(request, db)
    if user is None:
        return None
    attach_request_user_context(request, db, user)
    membership = db.query(OrganizationMembership.id).filter(
        OrganizationMembership.organization_id == _organization_id(user),
        OrganizationMembership.user_id == user.id,
    ).first()
    if membership is None:
        raise HTTPException(status_code=403, detail="Workspace membership is required")
    return user


def _organization_id(user) -> int:
    return getattr(user, "current_organization_id", None) or user.organization_id


def _workspace(db: Session, user) -> Workspace:
    workspace = db.query(Workspace).filter(
        Workspace.organization_id == _organization_id(user)
    ).one_or_none()
    if workspace is None:
        raise HTTPException(status_code=403, detail="Workspace access is required")
    return workspace


def _form_values(values: Mapping[str, Any] | None) -> dict[str, str]:
    values = values or {}
    return {
        "title": str(values.get("title") or ""),
        "client": str(values.get("client") or ""),
        "response_deadline": str(values.get("response_deadline") or ""),
        "solicitation_number": str(values.get("solicitation_number") or ""),
        "opportunity_type": str(values.get("opportunity_type") or "RFP"),
        "canonical_type": str(values.get("canonical_type") or ""),
        "description": str(values.get("description") or ""),
    }


def _error_map(errors) -> dict[str, str]:
    return {error.field: error.message for error in errors}


def _review_context(
    request: Request,
    *,
    user,
    draft: OpportunityIntakeDraft,
    values: Mapping[str, Any] | None = None,
    errors: Mapping[str, str] | None = None,
    page_error: str | None = None,
    exact_matches=(),
    probable_matches=(),
    confirm_probable: bool = False,
) -> dict[str, Any]:
    return {
        "request": request,
        "user": user,
        "active_page": "opportunity_intake",
        "draft": draft,
        "form_values": _form_values(values if values is not None else draft.candidate_fields_json),
        "form_errors": dict(errors or {}),
        "page_error": page_error,
        "exact_matches": exact_matches,
        "probable_matches": probable_matches,
        "confirm_probable": confirm_probable,
        "add_to_shortlist": bool(draft.add_to_shortlist),
        "csrf_token": intake_csrf_token(user.id, action="publish", draft_id=draft.id),
        "extraction_warnings": list((draft.extraction_metadata_json or {}).get("warnings") or []),
    }


def _start_context(request: Request, *, user, page_error: str | None = None) -> dict[str, Any]:
    return {
        "request": request,
        "user": user,
        "active_page": "opportunity_intake",
        "csrf_token": intake_csrf_token(user.id, action="start_manual"),
        "document_csrf_token": intake_csrf_token(user.id, action="upload_document"),
        "email_csrf_token": intake_csrf_token(user.id, action="upload_email"),
        "page_error": page_error,
        "max_upload_mb": max(1, config.SOURCE_MATERIAL_MAX_BYTES // (1024 * 1024)),
    }


def _download_disposition(filename: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(filename or "upload")).strip("._") or "upload"
    return f"attachment; filename=\"{safe[:120]}\"; filename*=UTF-8''{quote(filename or 'upload', safe='')}"


@router.get("/opportunities/new")
def new_opportunity(request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    _workspace(db, user)
    return templates.TemplateResponse(
        "opportunity_intake_start.html", _start_context(request, user=user)
    )


@router.post("/opportunity-intake/manual")
def start_manual_intake(
    request: Request,
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if not validate_intake_csrf_token(csrf_token, user.id, action="start_manual"):
        raise HTTPException(status_code=403, detail="Invalid form token")
    workspace = _workspace(db, user)
    draft = create_draft(
        db,
        organization_id=_organization_id(user),
        workspace_id=workspace.id,
        user_id=user.id,
        intake_method="manual",
        add_to_shortlist=True,
        publish_idempotency_key=secrets.token_urlsafe(24),
    )
    db.commit()
    return RedirectResponse(
        url=f"/opportunity-intake/{draft.id}/review", status_code=303
    )


@router.post("/opportunity-intake/document")
async def upload_rfp_document(
    request: Request,
    csrf_token: str = Form(""),
    document: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if not validate_intake_csrf_token(csrf_token, user.id, action="upload_document"):
        raise HTTPException(status_code=403, detail="Invalid form token")
    workspace = _workspace(db, user)
    content = await document.read(config.SOURCE_MATERIAL_MAX_BYTES + 1)
    try:
        storage = configured_source_material_storage()
        result = process_rfp_document(
            db,
            storage,
            organization_id=_organization_id(user),
            workspace_id=workspace.id,
            user_id=user.id,
            filename=document.filename,
            mime_type=document.content_type,
            content=content,
        )
    except IntakeDocumentError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "opportunity_intake_start.html",
            _start_context(request, user=user, page_error=str(exc)),
            status_code=422,
        )
    except SourceMaterialStorageError:
        db.rollback()
        return templates.TemplateResponse(
            "opportunity_intake_start.html",
            _start_context(
                request,
                user=user,
                page_error="Document storage is not available. Contact your workspace administrator.",
            ),
            status_code=503,
        )
    return RedirectResponse(
        url=f"/opportunity-intake/{result.draft.id}/review", status_code=303
    )


@router.post("/opportunity-intake/email")
async def upload_email_file(
    request: Request,
    csrf_token: str = Form(""),
    email_file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if not validate_intake_csrf_token(csrf_token, user.id, action="upload_email"):
        raise HTTPException(status_code=403, detail="Invalid form token")
    workspace = _workspace(db, user)
    content = await email_file.read(config.SOURCE_MATERIAL_MAX_BYTES + 1)
    try:
        result = process_email_file(
            db,
            configured_source_material_storage(),
            organization_id=_organization_id(user),
            workspace_id=workspace.id,
            user_id=user.id,
            filename=email_file.filename,
            mime_type=email_file.content_type,
            content=content,
        )
    except IntakeEmailError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "opportunity_intake_start.html",
            _start_context(request, user=user, page_error=str(exc)),
            status_code=422,
        )
    except SourceMaterialStorageError:
        db.rollback()
        return templates.TemplateResponse(
            "opportunity_intake_start.html",
            _start_context(
                request,
                user=user,
                page_error="Email storage is not available. Contact your workspace administrator.",
            ),
            status_code=503,
        )
    return RedirectResponse(
        url=f"/opportunity-intake/{result.draft.id}/review", status_code=303
    )


@router.get("/source-materials/{material_id}/download")
def download_source_material(
    request: Request,
    material_id: int,
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    workspace = _workspace(db, user)
    material = db.query(OpportunitySourceMaterial).filter(
        OpportunitySourceMaterial.id == material_id,
        OpportunitySourceMaterial.organization_id == _organization_id(user),
        OpportunitySourceMaterial.workspace_id == workspace.id,
    ).one_or_none()
    authorized = False
    if material is not None and material.opportunity_id is not None:
        opportunity = db.query(Opportunity).filter(
            Opportunity.id == material.opportunity_id,
            Opportunity.organization_id == _organization_id(user),
        ).one_or_none()
        authorized = opportunity is not None and (
            opportunity.qualification_status == "qualified"
            or getattr(user, "current_role", "member") == "admin"
        )
    elif material is not None and material.intake_draft_id is not None:
        authorized = db.query(OpportunityIntakeDraft.id).filter(
            OpportunityIntakeDraft.id == material.intake_draft_id,
            OpportunityIntakeDraft.organization_id == _organization_id(user),
            OpportunityIntakeDraft.workspace_id == workspace.id,
            OpportunityIntakeDraft.created_by_user_id == user.id,
        ).first() is not None
    if material is None or not authorized:
        logger.warning(
            "source_material_retrieval_denied material_id=%s organization_id=%s workspace_id=%s user_id=%s",
            material_id,
            _organization_id(user),
            workspace.id,
            user.id,
        )
        raise HTTPException(status_code=404, detail="Source material not found")
    try:
        storage = configured_source_material_storage()
        metadata = storage.metadata(material.storage_key)
    except SourceMaterialStorageError as exc:
        logger.error(
            "source_material_retrieval_failed material_id=%s organization_id=%s workspace_id=%s error_type=storage",
            material.id,
            material.organization_id,
            material.workspace_id,
        )
        raise HTTPException(status_code=503, detail="Source material is temporarily unavailable") from exc
    filename = material.original_filename or "source-material"
    logger.info(
        "source_material_retrieval_authorized material_id=%s organization_id=%s workspace_id=%s bytes=%s",
        material.id,
        material.organization_id,
        material.workspace_id,
        metadata.content_length,
    )
    return StreamingResponse(
        storage.iter_bytes(material.storage_key),
        media_type=material.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": _download_disposition(filename),
            "Content-Length": str(metadata.content_length),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/opportunity-intake/{draft_id}/review")
def review_manual_intake(
    request: Request,
    draft_id: int,
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    workspace = _workspace(db, user)
    try:
        draft = get_draft(
            db,
            draft_id=draft_id,
            organization_id=_organization_id(user),
            workspace_id=workspace.id,
            user_id=user.id,
        )
    except DraftAccessError as exc:
        raise HTTPException(status_code=404, detail="Opportunity form not found") from exc
    if draft.status == "PUBLISHED" and draft.published_opportunity_id:
        return RedirectResponse(
            url=f"/opportunity/{draft.published_opportunity_id}?return_to=feed", status_code=303
        )
    return templates.TemplateResponse(
        "opportunity_intake_review.html",
        _review_context(request, user=user, draft=draft),
    )


@router.post("/opportunity-intake/{draft_id}/publish")
def publish_manual_intake(
    request: Request,
    draft_id: int,
    csrf_token: str = Form(""),
    title: str = Form(""),
    client: str = Form(""),
    response_deadline: str = Form(""),
    solicitation_number: str = Form(""),
    opportunity_type: str = Form("RFP"),
    canonical_type: str = Form(""),
    description: str = Form(""),
    add_to_shortlist: str | None = Form(None),
    confirm_probable_duplicates: str | None = Form(None),
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    workspace = _workspace(db, user)
    try:
        draft = get_draft(
            db,
            draft_id=draft_id,
            organization_id=_organization_id(user),
            workspace_id=workspace.id,
            user_id=user.id,
        )
    except DraftAccessError as exc:
        raise HTTPException(status_code=404, detail="Opportunity form not found") from exc
    if not validate_intake_csrf_token(
        csrf_token, user.id, action="publish", draft_id=draft.id
    ):
        raise HTTPException(status_code=403, detail="Invalid form token")
    if draft.status == "PUBLISHED" and draft.published_opportunity_id:
        status = "shortlisted" if draft.add_to_shortlist else "feed"
        return RedirectResponse(
            url=f"/opportunity/{draft.published_opportunity_id}?{urlencode({'return_to': 'feed', 'intake_published': status})}",
            status_code=303,
        )

    reviewed = {
        "title": title,
        "client": client,
        "response_deadline": response_deadline,
        "solicitation_number": solicitation_number,
        "opportunity_type": opportunity_type,
        "canonical_type": canonical_type,
        "description": description,
    }
    shortlist_selected = add_to_shortlist is not None
    validation = validate_candidate(reviewed)
    update_draft(
        db,
        draft_id=draft.id,
        organization_id=draft.organization_id,
        workspace_id=draft.workspace_id,
        user_id=user.id,
        candidate=reviewed,
        status="READY" if validation.is_valid else "DRAFT",
        validation_errors=[asdict(error) for error in validation.errors],
        add_to_shortlist=shortlist_selected,
    )
    db.commit()
    if not validation.is_valid:
        return templates.TemplateResponse(
            "opportunity_intake_review.html",
            _review_context(
                request,
                user=user,
                draft=draft,
                values=reviewed,
                errors=_error_map(validation.errors),
            ),
            status_code=422,
        )

    duplicates = find_publication_duplicates(db, draft=draft, candidate=validation.candidate)
    if duplicates.exact_matches:
        return templates.TemplateResponse(
            "opportunity_intake_review.html",
            _review_context(
                request,
                user=user,
                draft=draft,
                values=reviewed,
                page_error="This appears to duplicate an opportunity already in BidLens.",
                exact_matches=duplicates.exact_matches,
            ),
            status_code=409,
        )
    if duplicates.probable_matches and confirm_probable_duplicates != "1":
        return templates.TemplateResponse(
            "opportunity_intake_review.html",
            _review_context(
                request,
                user=user,
                draft=draft,
                values=reviewed,
                probable_matches=duplicates.probable_matches,
                confirm_probable=True,
            ),
            status_code=200,
        )

    try:
        result = OpportunityPublisher.publish_reviewed_draft(
            db,
            draft_id=draft.id,
            publishing_user=user,
            reviewed_candidate=reviewed,
            add_to_shortlist=shortlist_selected,
            idempotency_key=draft.publish_idempotency_key,
        )
    except OpportunityDuplicateError as exc:
        return templates.TemplateResponse(
            "opportunity_intake_review.html",
            _review_context(
                request,
                user=user,
                draft=draft,
                values=reviewed,
                page_error="This opportunity was just matched to an existing BidLens opportunity.",
                exact_matches=exc.duplicates.exact_matches,
            ),
            status_code=409,
        )
    except OpportunityPublicationValidationError as exc:
        return templates.TemplateResponse(
            "opportunity_intake_review.html",
            _review_context(
                request, user=user, draft=draft, values=reviewed, errors=_error_map(exc.errors)
            ),
            status_code=422,
        )
    except OpportunityPublicationConflict:
        refreshed = db.get(OpportunityIntakeDraft, draft.id)
        if refreshed and refreshed.published_opportunity_id:
            result_id = refreshed.published_opportunity_id
            status = "shortlisted" if refreshed.add_to_shortlist else "feed"
            return RedirectResponse(
                url=f"/opportunity/{result_id}?{urlencode({'return_to': 'feed', 'intake_published': status})}",
                status_code=303,
            )
        raise

    status = "shortlisted" if result.added_to_shortlist else "feed"
    return RedirectResponse(
        url=f"/opportunity/{result.opportunity_id}?{urlencode({'return_to': 'feed', 'intake_published': status})}",
        status_code=303,
    )
