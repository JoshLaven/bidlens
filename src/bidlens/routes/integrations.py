from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import config
from ..auth import attach_request_user_context, get_current_user
from ..database import get_db
from ..models import IngestionRun, OrgProfile
from ..services.govwin import GovWinAdapter
from ..services.govwin_import import upsert_govwin_opportunity
from ..services.ingestion_details import build_error_detail, build_upsert_detail
from ..services.ingestion_runs import record_source_activity
from ..services.integration_credentials import decrypt_credentials, encrypt_credentials
from ..services.salesforce import SalesforceConfigError, SalesforceService


router = APIRouter()
templates = Jinja2Templates(directory="src/bidlens/templates")
GOVWIN_CREDENTIAL_FIELDS = ("client_id", "client_secret", "username", "password")


def _require_admin(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None
    attach_request_user_context(request, db, user)
    if getattr(user, "current_role", "member") != "admin":
        return False
    return user


def _user_org_id(user) -> int:
    return getattr(user, "current_organization_id", None) or user.organization_id


def _org_profile(db: Session, organization_id: int, *, create: bool = False) -> OrgProfile | None:
    profile = db.query(OrgProfile).filter(OrgProfile.org_id == organization_id).first()
    if profile is None and create:
        profile = OrgProfile(org_id=organization_id)
        db.add(profile)
        db.flush()
    return profile


def _admin_or_redirect(request: Request, db: Session):
    user = _require_admin(request, db)
    if user is None:
        return None, RedirectResponse(url="/login", status_code=303)
    if user is False:
        return None, RedirectResponse(url="/", status_code=303)
    return user, None


def _latest_run(db: Session, organization_id: int, sources: tuple[str, ...]) -> IngestionRun | None:
    return (
        db.query(IngestionRun)
        .filter(
            IngestionRun.organization_id == organization_id,
            IngestionRun.source.in_(sources),
        )
        .order_by(IngestionRun.started_at.desc(), IngestionRun.id.desc())
        .first()
    )


def _run_status(run: IngestionRun | None) -> str | None:
    if run is None:
        return None
    return "Error" if (run.error_count or 0) else "Success"


def _salesforce_connected() -> bool:
    try:
        return SalesforceService().is_authorized()
    except SalesforceConfigError:
        return False


def _redirect_with_status(path: str, **params) -> RedirectResponse:
    query = urlencode({key: value for key, value in params.items() if value is not None})
    return RedirectResponse(url=f"{path}?{query}" if query else path, status_code=303)


@router.get("/integrations")
async def integrations_page(request: Request, db: Session = Depends(get_db)):
    user, redirect = _admin_or_redirect(request, db)
    if redirect:
        return redirect

    organization_id = _user_org_id(user)
    profile = _org_profile(db, organization_id)
    sam_run = _latest_run(db, organization_id, ("sam.gov", "sam"))
    grants_run = _latest_run(db, organization_id, ("grants.gov", "grants_gov"))
    govwin_run = _latest_run(db, organization_id, ("govwin_api",))

    return templates.TemplateResponse("integrations.html", {
        "request": request,
        "user": user,
        "active_page": "integrations",
        "profile": profile,
        "govwin_credentials_saved": bool(profile and profile.govwin_credentials_encrypted),
        "sam_connected": bool(config.SAM_API_KEY),
        "grants_connected": bool(config.GRANTS_GOV_API_KEY),
        "salesforce_connected": _salesforce_connected(),
        "sam_run": sam_run,
        "sam_sync_status": _run_status(sam_run),
        "grants_run": grants_run,
        "grants_sync_status": _run_status(grants_run),
        "govwin_run": govwin_run,
        "govwin_sync_status": _run_status(govwin_run),
    })


@router.get("/integrations/govwin")
async def govwin_configuration_page(request: Request, db: Session = Depends(get_db)):
    user, redirect = _admin_or_redirect(request, db)
    if redirect:
        return redirect
    profile = _org_profile(db, _user_org_id(user))
    return templates.TemplateResponse("govwin_integration.html", {
        "request": request,
        "user": user,
        "active_page": "integrations",
        "profile": profile,
        "credentials_saved": bool(profile and profile.govwin_credentials_encrypted),
        "saved": request.query_params.get("saved") == "1",
        "test_result": request.query_params.get("test"),
        "sync_result": request.query_params.get("sync"),
        "message": request.query_params.get("message"),
    })


@router.post("/integrations/govwin")
async def save_govwin_configuration(
    request: Request,
    client_id: str = Form(""),
    client_secret: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    user, redirect = _admin_or_redirect(request, db)
    if redirect:
        return redirect
    profile = _org_profile(db, _user_org_id(user), create=True)
    credentials = decrypt_credentials(profile.govwin_credentials_encrypted)
    submitted = {
        "client_id": client_id.strip(),
        "client_secret": client_secret.strip(),
        "username": username.strip(),
        "password": password,
    }
    for key, value in submitted.items():
        if value:
            credentials[key] = value

    missing = [key for key in GOVWIN_CREDENTIAL_FIELDS if not credentials.get(key)]
    if missing:
        return _redirect_with_status(
            "/integrations/govwin",
            message="Enter all GovWin credential fields before saving.",
        )

    profile.govwin_credentials_encrypted = encrypt_credentials(credentials)
    profile.govwin_connection_status = "not_tested"
    db.commit()
    return _redirect_with_status("/integrations/govwin", saved="1")


@router.post("/integrations/govwin/test")
async def test_govwin_connection(request: Request, db: Session = Depends(get_db)):
    user, redirect = _admin_or_redirect(request, db)
    if redirect:
        return redirect
    profile = _org_profile(db, _user_org_id(user))
    credentials = decrypt_credentials(
        profile.govwin_credentials_encrypted if profile else None
    )
    result = GovWinAdapter(credentials).test_connection()
    if profile:
        profile.govwin_connection_status = result["status"]
        profile.govwin_last_tested_at = datetime.utcnow()
        db.commit()
    return _redirect_with_status(
        "/integrations/govwin",
        test="success" if result["connected"] else "error",
        message=result["message"],
    )


@router.post("/integrations/govwin/sync")
async def run_govwin_sync(request: Request, db: Session = Depends(get_db)):
    user, redirect = _admin_or_redirect(request, db)
    if redirect:
        return redirect

    organization_id = _user_org_id(user)
    profile = _org_profile(db, organization_id)
    credentials = decrypt_credentials(
        profile.govwin_credentials_encrypted if profile else None
    )
    adapter = GovWinAdapter(credentials)
    connection = adapter.test_connection()
    if not connection["connected"]:
        return _redirect_with_status(
            "/integrations/govwin",
            sync="error",
            message=connection["message"],
        )

    result = {
        "processed": 0,
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        "errors": 0,
        "_record_details": [],
    }
    for raw_opportunity in adapter.sync_saved_search():
        result["processed"] += 1
        normalized = adapter.normalize_opportunity(raw_opportunity)
        audit: dict[str, Any] = {}
        try:
            status, _opportunity, _diagnostic, reason_code = upsert_govwin_opportunity(
                db,
                organization_id,
                normalized,
                audit=audit,
            )
            result[status if status in result else "skipped"] += 1
            result["_record_details"].append(build_upsert_detail(
                source="govwin_api",
                data=normalized,
                status=status,
                audit=audit,
                reason_code=reason_code,
            ))
        except Exception as exc:
            result["errors"] += 1
            result["_record_details"].append(build_error_detail(
                source="govwin_api",
                source_record_id=normalized.get("source_record_id"),
                title=normalized.get("title"),
                error=exc,
            ))

    now = datetime.utcnow()
    profile.govwin_connection_status = "connected"
    profile.govwin_last_sync_at = now
    profile.govwin_last_sync_status = "success"
    record_source_activity(
        db,
        source="govwin_api",
        organization_id=organization_id,
        user_id=user.id,
        filename="Mock saved search sync",
        result=result,
        notes="Mock GovWin Web Services sync",
    )
    db.commit()
    summary = (
        f"Mock sync completed: {result['created']} created, "
        f"{result['updated']} updated, {result['unchanged']} unchanged."
    )
    return _redirect_with_status(
        "/integrations/govwin",
        sync="success",
        message=summary,
    )
