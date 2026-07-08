from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..auth import attach_request_user_context, get_current_user
from ..database import get_db
from ..models import Event, OrgProfile, SamSourceConfig
from ..services.sam_source_config import (
    SAM_NOTICE_TYPES,
    SamConfigValidationError,
    config_form_values,
    validate_sam_config_input,
)
from ..services.salesforce import SalesforceService


router = APIRouter()
templates = Jinja2Templates(directory="src/bidlens/templates")


def require_admin(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None
    attach_request_user_context(request, db, user)
    if getattr(user, "current_role", "member") != "admin":
        raise HTTPException(status_code=403, detail="Only organization admins can configure opportunity sources.")
    return user


def _user_org_id(user) -> int:
    return getattr(user, "current_organization_id", None) or user.organization_id


def _org_query(request: Request, org_id: int) -> str:
    return urlencode({"org_id": request.query_params.get("org_id") or str(org_id)})


def _source_context(db: Session, org_id: int) -> dict:
    sam_configs = (
        db.query(SamSourceConfig)
        .filter(SamSourceConfig.organization_id == org_id)
        .order_by(SamSourceConfig.name.asc(), SamSourceConfig.id.asc())
        .all()
    )
    profile = db.query(OrgProfile).filter(OrgProfile.org_id == org_id).first()
    grants_enabled = (
        db.query(Event.id)
        .filter(
            Event.org_id == org_id,
            Event.event_type == "opportunity_source_enabled",
            Event.payload["source"].as_string() == "grants.gov",
        )
        .first()
    )
    return {
        "sam": {
            "connected": bool(sam_configs),
            "configs": sam_configs,
            "description": "Discover federal contract notices and solicitations from SAM.gov saved searches.",
        },
        "grants": {
            "connected": bool(grants_enabled),
            "description": "Discover federal grant opportunities from Grants.gov using BidLens defaults.",
        },
        "govwin": {
            "connected": bool(profile and profile.govwin_credentials_encrypted),
            "description": "Bring GovWin opportunities into BidLens. Guided setup is coming soon.",
        },
    }


@router.get("/connect-sources")
async def connect_sources_page(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    org_id = _user_org_id(user)
    return templates.TemplateResponse("connect_sources.html", {
        "request": request,
        "user": user,
        "active_page": "connect_sources",
        "sources": _source_context(db, org_id),
        "saved_source": request.query_params.get("saved"),
    })


@router.get("/outbound-integrations")
async def outbound_integrations_page(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    salesforce = SalesforceService()
    return templates.TemplateResponse("outbound_integrations.html", {
        "request": request,
        "user": user,
        "active_page": "outbound_integrations",
        "salesforce_connected": salesforce.has_stored_authorization,
        "salesforce_instance_url": salesforce.instance_url,
    })


@router.get("/connect-sources/sam")
async def connect_sam_page(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    org_id = _user_org_id(user)
    config = (
        db.query(SamSourceConfig)
        .filter(SamSourceConfig.organization_id == org_id)
        .order_by(SamSourceConfig.name.asc(), SamSourceConfig.id.asc())
        .first()
    )
    return templates.TemplateResponse("connect_sam.html", {
        "request": request,
        "user": user,
        "active_page": "connect_sources",
        "form": config_form_values(config),
        "errors": {},
        "notice_types": SAM_NOTICE_TYPES,
        "config": config,
    })


@router.post("/connect-sources/grants/enable")
async def enable_grants_source(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    org_id = _user_org_id(user)
    already_enabled = (
        db.query(Event.id)
        .filter(
            Event.org_id == org_id,
            Event.event_type == "opportunity_source_enabled",
            Event.payload["source"].as_string() == "grants.gov",
        )
        .first()
    )
    if not already_enabled:
        db.add(Event(
            org_id=org_id,
            user_id=user.id,
            opp_id=None,
            event_type="opportunity_source_enabled",
            ui_version="setup_v1",
            payload={
                "source": "grants.gov",
                "configuration_flow": "one_click_enable",
                "default_configuration": {
                    "days_back": 1,
                    "rows": 25,
                    "authentication": "public_api",
                },
            },
        ))
        db.add(Event(
            org_id=org_id,
            user_id=user.id,
            opp_id=None,
            event_type="opportunity_sources_connected",
            ui_version="setup_v1",
            payload={
                "source": "grants.gov",
                "configuration_flow": "one_click_enable",
            },
        ))
        db.commit()

    query = _org_query(request, org_id)
    return RedirectResponse(url=f"/connect-sources?{query}&saved=grants", status_code=303)


@router.post("/connect-sources/sam")
async def save_connect_sam(
    request: Request,
    search_name: str = Form("Primary SAM.gov Search"),
    naics_codes: str = Form(...),
    keywords: str = Form(""),
    notice_types: list[str] = Form(default=[]),
    posted_days_back: str = Form("30"),
    max_records: str = Form("100"),
    db: Session = Depends(get_db),
):
    user = require_admin(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    org_id = _user_org_id(user)
    config = (
        db.query(SamSourceConfig)
        .filter(SamSourceConfig.organization_id == org_id)
        .order_by(SamSourceConfig.name.asc(), SamSourceConfig.id.asc())
        .first()
    )
    raw_values = {
        "search_name": search_name,
        "naics_codes": naics_codes,
        "keywords": keywords,
        "agencies": "",
        "set_asides": "",
        "notice_types": notice_types,
        "posted_days_back": posted_days_back,
        "due_days_from": "",
        "due_days_to": "",
        "active_only": True,
        "max_records": max_records,
    }
    try:
        values = validate_sam_config_input(**raw_values)
    except SamConfigValidationError as exc:
        return templates.TemplateResponse(
            "connect_sam.html",
            {
                "request": request,
                "user": user,
                "active_page": "connect_sources",
                "form": raw_values,
                "errors": exc.errors,
                "notice_types": SAM_NOTICE_TYPES,
                "config": config,
            },
            status_code=422,
        )

    if config is None:
        config = SamSourceConfig(organization_id=org_id)
        db.add(config)
    for field_name, value in values.items():
        setattr(config, field_name, value)

    db.add(Event(
        org_id=org_id,
        user_id=user.id,
        opp_id=None,
        event_type="opportunity_sources_connected",
        ui_version="setup_v1",
        payload={
            "source": "sam.gov",
            "configuration_flow": "connect_sources",
        },
    ))
    db.commit()

    query = _org_query(request, org_id)
    return RedirectResponse(url=f"/connect-sources?{query}&saved=sam", status_code=303)
