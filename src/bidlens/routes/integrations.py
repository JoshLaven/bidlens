from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import requests
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import config
from ..auth import attach_request_user_context, get_current_user
from ..database import get_db
from ..grants_gov_client import DEFAULT_GRANTS_POSTED_DAYS_BACK
from ..models import GrantsSourceConfig, IngestionRun, Opportunity, OrgProfile, SamSourceConfig
from ..services.govwin import GovWinAdapter
from ..services.govwin_import import upsert_govwin_opportunity
from ..services.opportunity_stages import is_excluded_govwin_stage
from ..services.ingestion_details import build_error_detail, build_invalid_detail, build_upsert_detail
from ..services.ingestion_runs import record_source_activity
from ..services.integration_credentials import decrypt_credentials, encrypt_credentials
from ..services.platform import post_setup_completion_url
from ..services.salesforce import (
    PROSPECT_FEED_STATUS,
    SalesforceApiError,
    SalesforceConfigError,
    SalesforceService,
)


router = APIRouter()
templates = Jinja2Templates(directory="src/bidlens/templates")
GOVWIN_CREDENTIAL_FIELDS = ("client_id", "client_secret", "username", "password")
SAM_SCHEDULE_LABEL = "Daily at 01:00 UTC"


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


def _run_status(run: IngestionRun | None) -> dict[str, str]:
    if run is None:
        return {"label": "Not run", "tone": "neutral"}
    normalized = str(run.status or "").strip().lower()
    if normalized == "paused_rate_limit":
        return {"label": "Paused", "tone": "paused"}
    if normalized in {"running", "started", "in_progress"} or (
        run.finished_at is None and normalized not in {"failed", "partial_success"}
    ):
        return {"label": "Running", "tone": "running"}
    if normalized in {"failed", "error", "partial_success"} or (run.error_count or 0):
        return {"label": "Error", "tone": "error"}
    return {"label": "Success", "tone": "success"}


def _latest_successful_run(
    db: Session,
    organization_id: int,
    sources: tuple[str, ...],
) -> IngestionRun | None:
    return (
        db.query(IngestionRun)
        .filter(
            IngestionRun.organization_id == organization_id,
            IngestionRun.source.in_(sources),
            IngestionRun.status.in_(("success", "completed")),
            IngestionRun.error_count == 0,
        )
        .order_by(IngestionRun.started_at.desc(), IngestionRun.id.desc())
        .first()
    )


def _run_snapshot(
    db: Session,
    organization_id: int,
    sources: tuple[str, ...],
) -> dict[str, Any]:
    latest = _latest_run(db, organization_id, sources)
    return {
        "latest": latest,
        "status": _run_status(latest),
        "last_success": _latest_successful_run(db, organization_id, sources),
    }


def _salesforce_operational_snapshot() -> dict[str, Any]:
    service = SalesforceService()
    snapshot = {
        "connected": False,
        "instance_url": service.connected_instance_url,
        "inspection": None,
        "error": None,
    }
    try:
        snapshot["connected"] = service.is_authorized()
        snapshot["instance_url"] = service.connected_instance_url
        if snapshot["connected"]:
            snapshot["inspection"] = service.inspect_opportunity_requirements()
    except (SalesforceApiError, SalesforceConfigError, requests.RequestException) as exc:
        snapshot["error"] = str(exc)
    return snapshot


def _health_check(label: str, state: str, detail: str | None = None) -> dict[str, str | None]:
    return {"label": label, "state": state, "detail": detail}


def _run_time(run: IngestionRun | None) -> datetime | None:
    return (run.finished_at or run.started_at) if run else None


def _days_since(run: IngestionRun | None, *, now: datetime) -> int | None:
    timestamp = _run_time(run)
    if timestamp is None:
        return None
    if timestamp.tzinfo is not None and now.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=None)
    elif timestamp.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    return max(0, (now - timestamp).days)


def _apply_connector_health(
    center: dict[str, Any],
    *,
    now: datetime,
) -> None:
    sam = center["sam"]
    sam_checks = [
        _health_check(
            "SAM.gov API access configured",
            "pass" if sam["connected"] else "fail",
        ),
        _health_check(
            "API access validated by a successful pull",
            "pass" if sam["last_success"] else "warning",
        ),
        _health_check(
            "Saved search found",
            "pass" if sam["configs"] else "fail",
            f"{len(sam['configs'])} configured" if sam["configs"] else None,
        ),
        _health_check(
            f"{sam['naics_count']} NAICS configured",
            "pass" if sam["naics_count"] else "fail",
        ),
        _health_check(
            (
                f"{len(sam['notice_types'])} notice types configured"
                if sam["notice_types"]
                else "Default discovery notice types active"
            ),
            "pass",
        ),
        _health_check("Daily scheduler configured", "pass", sam["schedule"]),
    ]
    if not sam["connected"] or not sam["configs"] or not sam["naics_count"]:
        sam_health = {
            "level": "required",
            "label": "Configuration Required",
            "summary": "Complete SAM.gov access and saved-search criteria before pulls can run.",
        }
    elif sam["status"]["tone"] == "paused":
        sam_health = {
            "level": "paused",
            "label": "Paused",
            "summary": "Waiting for the SAM.gov daily quota reset.",
        }
    elif sam["status"]["tone"] == "running":
        sam_health = {
            "level": "healthy",
            "label": "Healthy",
            "summary": "A SAM.gov pull is currently running.",
        }
    elif sam["status"]["tone"] == "error":
        error_count = sam["latest"].error_count if sam["latest"] else 0
        sam_health = {
            "level": "warning",
            "label": "Needs Attention",
            "summary": (
                f"The latest pull completed with {error_count} error"
                f"{'' if error_count == 1 else 's'}."
            ),
        }
    elif sam["last_success"]:
        sam_health = {
            "level": "healthy",
            "label": "Healthy",
            "summary": "Configured and completing SAM.gov pulls successfully.",
        }
    else:
        sam_health = {
            "level": "warning",
            "label": "Needs Attention",
            "summary": "Configured, but no successful SAM.gov pull has been recorded.",
        }
    sam["health"] = {**sam_health, "checks": sam_checks}

    grants = center["grants"]
    grants_latest_ok = grants["status"]["tone"] == "success"
    grants_checks = [
        _health_check(
            "API reachable on the last successful pull",
            "pass" if grants["last_success"] else "warning",
        ),
        _health_check(
            "Daily ingestion configured",
            "pass" if grants["connected"] else "warning",
            grants["schedule"],
        ),
        _health_check("Pagination enabled", "pass", "All matching daily results"),
        _health_check(
            "Last pull successful",
            "pass" if grants_latest_ok else "warning",
        ),
    ]
    if not grants["connected"]:
        grants_health = {
            "level": "required",
            "label": "Configuration Required",
            "summary": "Enable Grants.gov before scheduled pulls can run.",
        }
    elif grants["status"]["tone"] == "error":
        grants_health = {
            "level": "warning",
            "label": "Needs Attention",
            "summary": "The latest Grants.gov pull reported an error.",
        }
    elif grants["last_success"]:
        grants_health = {
            "level": "healthy",
            "label": "Healthy",
            "summary": "The daily change feed is configured and the latest pull completed normally.",
        }
    else:
        grants_health = {
            "level": "warning",
            "label": "Needs Attention",
            "summary": "No successful Grants.gov pull has been recorded.",
        }
    grants["health"] = {**grants_health, "checks": grants_checks}

    govwin = center["govwin"]
    govwin_days = _days_since(govwin["last_success"], now=now)
    import_recent = govwin_days is not None and govwin_days <= 5
    govwin_checks = [
        _health_check("Manual import method configured", "pass", ".xlsx staging export"),
        _health_check("Stage mappings configured", "pass", "Forecast / RFI / RFP"),
        _health_check(
            "API credentials configured",
            "pass" if govwin["credentials_saved"] else "info",
            None if govwin["credentials_saved"] else "Optional while manual import is active",
        ),
        _health_check(
            (
                "Import completed within 5 days"
                if import_recent
                else "No import in the last 5 days"
            ),
            "pass" if import_recent else "warning",
        ),
    ]
    if govwin["status"]["tone"] == "error":
        govwin_health = {
            "level": "warning",
            "label": "Needs Attention",
            "summary": "The latest GovWin import reported an error.",
        }
    elif import_recent:
        govwin_health = {
            "level": "healthy",
            "label": "Healthy",
            "summary": "The manual import path is configured and was used recently.",
        }
    else:
        govwin_health = {
            "level": "warning",
            "label": "Needs Attention",
            "summary": "The import path is ready, but no recent GovWin import is recorded.",
        }
    govwin["health"] = {**govwin_health, "checks": govwin_checks}

    salesforce = center["salesforce"]
    inspection = salesforce.get("inspection") or {}
    requirements_valid = bool(
        inspection.get("required_fields_verified")
        and inspection.get("default_stage_valid")
        and inspection.get("selected_intake_source")
    )
    mappings_valid = bool(inspection.get("field_mappings_valid"))
    salesforce_checks = [
        _health_check(
            "OAuth valid",
            (
                "pass"
                if salesforce["connected"]
                else "warning" if salesforce.get("validation_error") else "fail"
            ),
            salesforce.get("validation_error"),
        ),
        _health_check(
            "Connected organization identified",
            "pass" if salesforce["connected"] and salesforce["instance_url"] else "fail",
            salesforce["instance_url"],
        ),
    ]
    if salesforce["connected"]:
        salesforce_checks.extend([
            _health_check(
                "Required Opportunity fields verified",
                "pass" if requirements_valid else "warning",
            ),
            _health_check(
                "Field mappings valid",
                "pass" if mappings_valid else "warning",
            ),
        ])
    if salesforce.get("validation_error"):
        salesforce_health = {
            "level": "warning",
            "label": "Needs Attention",
            "summary": "BidLens could not validate the Salesforce connection.",
        }
    elif not salesforce["connected"]:
        salesforce_health = {
            "level": "required",
            "label": "Configuration Required",
            "summary": "Authorize Salesforce before BidLens can create or update Opportunities.",
        }
    elif requirements_valid and mappings_valid:
        salesforce_health = {
            "level": "healthy",
            "label": "Healthy",
            "summary": "OAuth, required fields, and BidLens Opportunity mappings are valid.",
        }
    else:
        salesforce_health = {
            "level": "warning",
            "label": "Needs Attention",
            "summary": "Connected, but the Opportunity schema does not satisfy BidLens requirements.",
        }
    salesforce["health"] = {**salesforce_health, "checks": salesforce_checks}

    connectors = (sam, grants, govwin, salesforce)
    healthy_count = sum(
        connector["health"]["level"] == "healthy"
        for connector in connectors
    )
    paused_count = sum(
        connector["health"]["level"] == "paused"
        for connector in connectors
    )
    center["overall_health"] = {
        "total": len(connectors),
        "healthy": healthy_count,
        "paused": paused_count,
        "attention": len(connectors) - healthy_count - paused_count,
    }


def _configuration_center_context(
    db: Session,
    *,
    organization_id: int,
    profile: OrgProfile | None,
    salesforce_snapshot: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    sam_configs = (
        db.query(SamSourceConfig)
        .filter(SamSourceConfig.organization_id == organization_id)
        .order_by(SamSourceConfig.name.asc(), SamSourceConfig.id.asc())
        .all()
    )
    sam_naics_codes = sorted({
        code
        for source_config in sam_configs
        for code in (source_config.naics_codes or [])
    })
    sam_notice_types = list(dict.fromkeys(
        notice_type
        for source_config in sam_configs
        for notice_type in (source_config.notice_types or [])
    ))
    grants_config = (
        db.query(GrantsSourceConfig)
        .filter(
            GrantsSourceConfig.organization_id == organization_id,
            GrantsSourceConfig.enabled.is_(True),
        )
        .first()
    )
    last_salesforce_sync = (
        db.query(func.max(Opportunity.salesforce_synced_at))
        .filter(
            Opportunity.organization_id == organization_id,
            Opportunity.salesforce_synced_at.is_not(None),
        )
        .scalar()
    )

    center = {
        "sam": {
            "connected": bool(config.SAM_API_KEY),
            "configs": sam_configs,
            "default_config": sam_configs[0] if sam_configs else None,
            "naics_count": len(sam_naics_codes),
            "notice_types": sam_notice_types,
            "schedule": SAM_SCHEDULE_LABEL,
            **_run_snapshot(db, organization_id, ("sam.gov", "sam")),
        },
        "grants": {
            "connected": bool(grants_config),
            "config": grants_config,
            "date_window": f"Rolling {grants_config.posted_days_back if grants_config else DEFAULT_GRANTS_POSTED_DAYS_BACK}-day posted-date window",
            "statuses": ("Forecasted", "Posted"),
            "schedule": "Daily at 01:30 UTC" if grants_config else "Enable Grants.gov to schedule daily ingestion",
            **_run_snapshot(db, organization_id, ("grants.gov", "grants_gov")),
        },
        "govwin": {
            "connection_label": "Manual Import",
            "credentials_saved": bool(profile and profile.govwin_credentials_encrypted),
            "connection_status": (
                profile.govwin_connection_status
                if profile and profile.govwin_connection_status
                else "not_tested"
            ),
            "stage_mappings": (
                "Forecast Pre-RFP → Forecast",
                "Pre-RFP → RFI",
                "Post-RFP → RFP",
                "Source Selection → Excluded",
            ),
            **_run_snapshot(
                db,
                organization_id,
                ("govwin_export", "govwin_api"),
            ),
        },
        "salesforce": {
            "connected": bool(salesforce_snapshot.get("connected")),
            "instance_url": salesforce_snapshot.get("instance_url"),
            "inspection": salesforce_snapshot.get("inspection"),
            "validation_error": salesforce_snapshot.get("error"),
            "connection_health": (
                "Authorized"
                if salesforce_snapshot.get("connected")
                else "Not authorized"
            ),
            "last_sync": last_salesforce_sync,
            "mappings": (
                ("Default Stage", "Prospecting"),
                ("Default Intake Status", PROSPECT_FEED_STATUS),
                ("External Source ID", "External_Source_ID_c__c ← source_record_id"),
                ("Close Date", "Opportunity response deadline"),
            ),
        },
    }
    _apply_connector_health(center, now=now or datetime.utcnow())
    return center


def _redirect_with_status(
    path: str,
    *,
    db: Session | None = None,
    user=None,
    organization_id: int | None = None,
    **params,
) -> RedirectResponse:
    query = urlencode({key: value for key, value in params.items() if value is not None})
    live_url = f"{path}?{query}" if query else path
    if db is not None and user is not None:
        live_url = post_setup_completion_url(
            db,
            user,
            organization_id=organization_id or _user_org_id(user),
            live_url=live_url,
        )
    return RedirectResponse(url=live_url, status_code=303)


@router.get("/integrations")
async def integrations_page(request: Request, db: Session = Depends(get_db)):
    user, redirect = _admin_or_redirect(request, db)
    if redirect:
        return redirect

    organization_id = _user_org_id(user)
    profile = _org_profile(db, organization_id)
    salesforce_snapshot = _salesforce_operational_snapshot()
    center = _configuration_center_context(
        db,
        organization_id=organization_id,
        profile=profile,
        salesforce_snapshot=salesforce_snapshot,
    )

    return templates.TemplateResponse("integrations.html", {
        "request": request,
        "user": user,
        "active_page": "integrations",
        "profile": profile,
        "center": center,
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
            db=db,
            user=user,
            message="Enter all GovWin credential fields before saving.",
        )

    profile.govwin_credentials_encrypted = encrypt_credentials(credentials)
    profile.govwin_connection_status = "not_tested"
    db.commit()
    return _redirect_with_status("/integrations/govwin", db=db, user=user, saved="1")


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
        db=db,
        user=user,
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
            db=db,
            user=user,
            organization_id=organization_id,
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
        raw_stage = raw_opportunity.get("source_stage") or raw_opportunity.get("opportunity_type")
        if is_excluded_govwin_stage(raw_stage):
            result["skipped"] += 1
            result["_record_details"].append(build_invalid_detail(
                source="govwin_api",
                source_record_id=str(raw_opportunity.get("opportunity_id") or "").strip() or None,
                title=str(raw_opportunity.get("title") or "").strip() or None,
                reason="Source Selection opportunities are outside the discovery workflow",
            ))
            continue
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
        db=db,
        user=user,
        organization_id=organization_id,
        sync="success",
        message=summary,
    )
