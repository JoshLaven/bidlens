from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from urllib.parse import urlencode

from ..auth import attach_request_user_context, get_current_user
from ..database import get_db
from ..models import CompanyProfile, Event, Organization
from .opportunities import get_sidebar


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


def _clean_text(value: str | None) -> str:
    if isinstance(value, list):
        return "\n".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _clean_optional_text(value: str | None) -> str | None:
    text = _clean_text(value)
    return text or None


def _profile_payload() -> dict[str, object]:
    return {"profile_type": "organization_identity"}


def _profile_form(profile: CompanyProfile | None, organization: Organization | None = None) -> dict[str, str]:
    return {
        "organization_name": _clean_text(organization.name if organization else None),
        "website_url": _clean_text(profile.website_url if profile else None),
        "uei": _clean_text(profile.uei if profile else None),
        "cage_code": _clean_text(profile.cage_code if profile else None),
        "duns": _clean_text(profile.duns if profile else None),
    }


def _recent_work_context(profile: CompanyProfile | None) -> dict[str, object]:
    identifiers = []
    if profile and profile.uei:
        identifiers.append({"label": "UEI", "value": profile.uei})
    if profile and profile.cage_code:
        identifiers.append({"label": "CAGE", "value": profile.cage_code})
    if profile and profile.duns:
        identifiers.append({"label": "DUNS", "value": profile.duns})

    profile_json = profile.profile_json if profile and isinstance(profile.profile_json, dict) else {}
    recent_work = profile_json.get("recent_work") if isinstance(profile_json, dict) else None
    awards = recent_work.get("awards", []) if isinstance(recent_work, dict) else []
    status = recent_work.get("status") if isinstance(recent_work, dict) else None

    return {
        "identifiers": identifiers,
        "awards": awards,
        "status": status or ("ready" if identifiers else "needs_identifiers"),
        "requested": bool(recent_work and recent_work.get("requested_at")),
    }


def active_company_profile(db: Session, org_id: int) -> CompanyProfile | None:
    return (
        db.query(CompanyProfile)
        .filter(
            CompanyProfile.org_id == org_id,
            CompanyProfile.archived_at.is_(None),
        )
        .order_by(CompanyProfile.updated_at.desc(), CompanyProfile.id.desc())
        .first()
    )


def archive_duplicate_active_profiles(
    db: Session,
    *,
    org_id: int,
    keep_profile_id: int | None = None,
) -> int:
    query = db.query(CompanyProfile).filter(
        CompanyProfile.org_id == org_id,
        CompanyProfile.archived_at.is_(None),
    )
    profiles = query.order_by(CompanyProfile.updated_at.desc(), CompanyProfile.id.desc()).all()
    if not profiles:
        return 0

    keeper = next((profile for profile in profiles if profile.id == keep_profile_id), profiles[0])
    archived_count = 0
    now = datetime.utcnow()
    for profile in profiles:
        if profile.id == keeper.id:
            continue
        profile.archived_at = now
        archived_count += 1
    return archived_count


def upsert_company_profile(
    db: Session,
    *,
    org_id: int,
    website_url: str = "",
    uei: str = "",
    cage_code: str = "",
    duns: str = "",
) -> tuple[CompanyProfile, bool, int]:
    payload = _profile_payload()
    profile = active_company_profile(db, org_id)
    created = profile is None
    if profile is None:
        profile = CompanyProfile(org_id=org_id, profile_json=payload)
        db.add(profile)
        db.flush()

    organization = db.query(Organization).filter(Organization.id == org_id).first()
    profile.company_name = organization.name if organization else profile.company_name
    profile.website_url = _clean_optional_text(website_url)
    profile.uei = _clean_optional_text(uei)
    profile.cage_code = _clean_optional_text(cage_code)
    profile.duns = _clean_optional_text(duns)
    existing_recent_work = (
        profile.profile_json.get("recent_work")
        if isinstance(profile.profile_json, dict)
        else None
    )
    existing_awards = (
        existing_recent_work.get("awards", [])
        if isinstance(existing_recent_work, dict)
        else []
    )
    if profile.uei or profile.cage_code or profile.duns:
        payload["recent_work"] = {
            "status": "ready",
            "requested_at": datetime.utcnow().isoformat(),
            "identifiers": {
                "uei": profile.uei,
                "cage_code": profile.cage_code,
                "duns": profile.duns,
            },
            "awards": existing_awards,
        }
    profile.profile_json = payload

    archived_count = archive_duplicate_active_profiles(
        db,
        org_id=org_id,
        keep_profile_id=profile.id,
    )
    return profile, created, archived_count


def _record_profile_event(
    db: Session,
    *,
    org_id: int,
    user_id: int | None,
    created: bool,
    archived_duplicates: int,
) -> None:
    db.add(Event(
        org_id=org_id,
        user_id=user_id,
        opp_id=None,
        event_type="company_profile_configured",
        ui_version="v1",
        payload={
            "source": "company_profile",
            "created": created,
            "archived_duplicate_profiles": archived_duplicates,
        },
    ))


def render_company_profile(
    request: Request,
    db: Session,
    user,
    *,
    profile: CompanyProfile | None,
    form: dict[str, str] | None = None,
    saved: bool = False,
    duplicate_cleanup_count: int = 0,
):
    org_id = _user_org_id(user)
    organization = db.query(Organization).filter(Organization.id == org_id).first()
    form = form or _profile_form(profile, organization)
    return templates.TemplateResponse(
        "company_profile.html",
        {
            "request": request,
            "user": user,
            "active_page": "company_profile",
            "sidebar": get_sidebar(db, user),
            "profile": profile,
            "profile_form": form,
            "recent_work": _recent_work_context(profile),
            "saved": saved,
            "duplicate_cleanup_count": duplicate_cleanup_count,
        },
    )


@router.get("/company-profile")
async def company_profile_page(
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    org_id = _user_org_id(user)
    profile = active_company_profile(db, org_id)
    duplicate_cleanup_count = archive_duplicate_active_profiles(
        db,
        org_id=org_id,
        keep_profile_id=profile.id if profile else None,
    )
    if duplicate_cleanup_count:
        db.commit()
        profile = active_company_profile(db, org_id)

    return render_company_profile(
        request,
        db,
        user,
        profile=profile,
        duplicate_cleanup_count=duplicate_cleanup_count,
    )


@router.post("/company-profile")
async def company_profile_save(
    request: Request,
    website_url: str = Form(""),
    uei: str = Form(""),
    cage_code: str = Form(""),
    duns: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    org_id = _user_org_id(user)
    profile, created, archived_count = upsert_company_profile(
        db,
        org_id=org_id,
        website_url=website_url,
        uei=uei,
        cage_code=cage_code,
        duns=duns,
    )
    _record_profile_event(
        db,
        org_id=org_id,
        user_id=getattr(user, "id", None),
        created=created,
        archived_duplicates=archived_count,
    )
    db.commit()

    query = urlencode({
        key: value
        for key, value in {
            "org_id": request.query_params.get("org_id") or str(org_id),
            "profile_saved": "1",
        }.items()
        if value
    })
    return RedirectResponse(url=f"/organization-setup?{query}", status_code=303)


@router.post("/company-profile/save")
async def company_profile_save_legacy(
    request: Request,
    website_url: str = Form(""),
    uei: str = Form(""),
    cage_code: str = Form(""),
    duns: str = Form(""),
    db: Session = Depends(get_db),
):
    return await company_profile_save(
        request,
        website_url=website_url,
        uei=uei,
        cage_code=cage_code,
        duns=duns,
        db=db,
    )


@router.post("/company-profile/generate")
async def company_profile_generate_removed():
    return RedirectResponse(url="/company-profile", status_code=303)


@router.get("/company-profile/{profile_id}")
async def company_profile_saved_detail(
    profile_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    org_id = _user_org_id(user)
    requested_profile = (
        db.query(CompanyProfile)
        .filter(
            CompanyProfile.id == profile_id,
            CompanyProfile.org_id == org_id,
            CompanyProfile.archived_at.is_(None),
        )
        .first()
    )
    if requested_profile:
        archive_duplicate_active_profiles(
            db,
            org_id=org_id,
            keep_profile_id=requested_profile.id,
        )
        db.commit()

    return RedirectResponse(url="/company-profile", status_code=303)
