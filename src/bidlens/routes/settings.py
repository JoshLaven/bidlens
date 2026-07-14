from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..auth import attach_request_user_context, get_current_user
from ..models import Event, OrgProfile, OrganizationMembership, PursuitLane
from ..services.platform import post_setup_completion_url
from ..services.pursuit_lanes import set_user_my_lanes, user_my_lanes

router = APIRouter()
templates = Jinja2Templates(directory="src/bidlens/templates")

def require_user(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None
    attach_request_user_context(request, db, user)
    return user


def _user_org_id(user) -> int:
    return getattr(user, "current_organization_id", None) or user.organization_id


def _current_user_role(db: Session, user) -> str:
    membership = (
        db.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == _user_org_id(user),
            OrganizationMembership.user_id == user.id,
        )
        .first()
    )
    return membership.role if membership else "member"


def _is_admin(user) -> bool:
    return getattr(user, "current_role", "member") == "admin"


def _settings_redirect_url(request: Request) -> str:
    query = str(request.url.query or "").strip()
    return f"/settings?{query}" if query else "/settings"


def _post_settings_save_url(request: Request, db: Session, user) -> str:
    return post_setup_completion_url(
        db,
        user,
        organization_id=_user_org_id(user),
        live_url=_settings_redirect_url(request),
    )


def _my_lanes_redirect_url(request: Request) -> str:
    params = [
        (key, value)
        for key, value in parse_qsl(str(request.url.query or ""), keep_blank_values=False)
        if key != "saved"
    ]
    params.append(("saved", "1"))
    return f"/my-settings/my-lanes?{urlencode(params)}"


@router.get("/my-settings")
async def my_settings_page(
    request: Request,
    db: Session = Depends(get_db)
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    return templates.TemplateResponse("my_settings.html", {
        "request": request,
        "user": user,
        "active_page": "my_settings",
    })


def _settings_placeholder_response(request: Request, user, title: str, description: str):
    return templates.TemplateResponse("my_settings_placeholder.html", {
        "request": request,
        "user": user,
        "title": title,
        "description": description,
        "active_page": "my_settings",
    })


@router.get("/my-settings/account")
async def my_account_settings_page(
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return _settings_placeholder_response(
        request,
        user,
        "Account",
        "Account preferences will live here in a future settings pass.",
    )


@router.get("/my-settings/notifications")
async def my_notification_settings_page(
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return _settings_placeholder_response(
        request,
        user,
        "Notifications",
        "Daily Brief and notification preferences will live here in a future settings pass.",
    )


@router.get("/my-settings/organization")
async def my_organization_settings_page(
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return _settings_placeholder_response(
        request,
        user,
        "My Organization",
        "Organization-specific settings are managed by Workspace Admins.",
    )


@router.get("/my-settings/my-lanes")
async def my_lanes_settings_page(
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    org_id = _user_org_id(user)
    lanes = (
        db.query(PursuitLane)
        .filter(
            PursuitLane.organization_id == org_id,
            PursuitLane.is_active.is_(True),
        )
        .order_by(PursuitLane.name.asc(), PursuitLane.id.asc())
        .all()
    )
    my_lanes = user_my_lanes(db, organization_id=org_id, user_id=user.id)
    return templates.TemplateResponse("my_lanes_settings.html", {
        "request": request,
        "user": user,
        "lanes": lanes,
        "my_lanes": my_lanes,
        "my_lane_ids": {lane.id for lane in my_lanes},
        "saved": request.query_params.get("saved") == "1",
        "active_page": "my_settings",
    })


@router.post("/my-settings/my-lanes")
async def save_my_lanes_settings(
    request: Request,
    lane_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    set_user_my_lanes(
        db,
        organization_id=_user_org_id(user),
        user_id=user.id,
        lane_ids=lane_ids,
    )
    db.commit()

    return RedirectResponse(url=_my_lanes_redirect_url(request), status_code=303)


@router.get("/administration")
async def administration_page(
    request: Request,
    db: Session = Depends(get_db)
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not _is_admin(user):
        return RedirectResponse(url="/", status_code=303)

    query = str(request.url.query or "").strip()
    suffix = f"?{query}" if query else ""
    return RedirectResponse(url=f"/company-profile{suffix}", status_code=303)


@router.get("/salesforce")
async def salesforce_admin_page(
    request: Request,
    db: Session = Depends(get_db)
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not _is_admin(user):
        return RedirectResponse(url="/", status_code=303)

    query = str(request.url.query or "").strip()
    suffix = f"?{query}" if query else ""
    return RedirectResponse(url=f"/integrations{suffix}#salesforce", status_code=303)

@router.get("/settings")
async def settings_page(
    request: Request,
    db: Session = Depends(get_db)
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    # V1: treat first user in org as "admin" if you don't have roles yet
    # If you add roles later, tighten this.
    profile = db.query(OrgProfile).filter(OrgProfile.org_id == _user_org_id(user)).first()

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "user": user,
        "profile": profile,
        "is_admin": _is_admin(user),
        "active_page": "settings",
    })

@router.post("/settings")
async def settings_save(
    request: Request,
    include_keywords: str = Form(None),
    exclude_keywords: str = Form(None),
    include_agencies: str = Form(None),
    exclude_agencies: str = Form(None),
    min_days_out: str = Form(None),
    max_days_out: str = Form(None),
    digest_max_items: str = Form(None),
    digest_recipients: str = Form(None),
    digest_time_local: str = Form(None),
    triage_enabled: str = Form(None),
    db: Session = Depends(get_db)
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    profile = db.query(OrgProfile).filter(OrgProfile.org_id == _user_org_id(user)).first()
    if not profile:
        profile = OrgProfile(org_id=_user_org_id(user))
        db.add(profile)
        db.flush()

    # Save (trim to keep it clean)
    profile.include_keywords = include_keywords.strip() if include_keywords else None
    profile.exclude_keywords = exclude_keywords.strip() if exclude_keywords else None
    profile.include_agencies = include_agencies.strip() if include_agencies else None
    profile.exclude_agencies = exclude_agencies.strip() if exclude_agencies else None

    def to_int(x):
        try:
            return int(x)
        except Exception:
            return None

    profile.min_days_out = to_int(min_days_out)
    profile.max_days_out = to_int(max_days_out)
    profile.digest_max_items = to_int(digest_max_items) or 20

    profile.digest_recipients = digest_recipients.strip() if digest_recipients else None
    profile.digest_time_local = digest_time_local.strip() if digest_time_local else None
    if _is_admin(user):
        profile.triage_enabled = triage_enabled == "1"

    org_id = _user_org_id(user)
    already_configured = (
        db.query(Event.id)
        .filter(Event.org_id == org_id, Event.event_type == "feed_rules_configured")
        .first()
    )
    if not already_configured:
        db.add(Event(
            org_id=org_id,
            user_id=user.id,
            opp_id=None,
            event_type="feed_rules_configured",
            ui_version="setup_v1",
            payload={"source": "settings"},
        ))

    db.commit()

    return RedirectResponse(url=_post_settings_save_url(request, db, user), status_code=303)
