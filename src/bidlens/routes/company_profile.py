import json
import logging
import os
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
import requests
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..config import DOTENV_PATH
from ..database import get_db
from ..models import CompanyProfile
from ..tenancy import current_org_id
from .opportunities import get_sidebar


router = APIRouter()
templates = Jinja2Templates(directory="src/bidlens/templates")
logger = logging.getLogger(__name__)

PROFILE_SCHEMA_KEYS = [
    "company_summary",
    "core_capabilities",
    "target_markets",
    "past_award_patterns",
    "agencies_served",
    "naics_seen",
    "fit_strengths",
    "fit_limitations",
    "evidence_summary",
    "confidence",
]


def require_user(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None
    setattr(user, "current_organization_id", current_org_id(request, db, user))
    return user


def _user_org_id(user) -> int:
    return getattr(user, "current_organization_id", None) or user.organization_id


def get_default_profile_sections():
    return {
        "company_summary": [
            "This is initial scaffolding for the future n8n-backed company profile workflow.",
            "The page is ready to display structured capability, agency, NAICS, and fit-pattern signals once profile JSON is wired in.",
        ],
        "core_capabilities": [
            "Program and project support services",
            "Acquisition and operations advisory support",
            "Documentation, analysis, and coordination work",
        ],
        "target_markets": [
            "Federal civilian agencies",
            "Defense and mission support organizations",
            "Professional services opportunities with repeat delivery patterns",
        ],
        "past_award_patterns": [
            "Sample signal: service-oriented contract work with recurring agency demand",
            "Sample signal: support scopes with structured deliverables and repeatable workflows",
        ],
        "agencies_served": [
            "Department of Defense",
            "Health and Human Services",
            "Department of Homeland Security",
        ],
        "naics_seen": [
            "541611 - Administrative Management and General Management Consulting Services",
            "541690 - Other Scientific and Technical Consulting Services",
            "541330 - Engineering Services",
        ],
        "fit_strengths": [
            "Good placeholder fit for advisory, coordination, and services-heavy opportunities",
            "Potential strength where written deliverables and repeatable execution matter",
        ],
        "fit_limitations": [
            "Limited placeholder evidence for manufacturing or hardware-centric work",
            "May be a weaker fit for highly specialized product procurement",
        ],
        "evidence_summary": [
            "Future versions should summarize supporting evidence from awards, solicitations, and company inputs.",
            "For now, this section is static scaffolding only.",
        ],
        "confidence": "medium",
    }


def normalize_profile_value(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def normalize_confidence(value):
    confidence = str(value or "medium").strip().lower()
    return confidence if confidence in {"high", "medium", "low"} else "medium"


def extract_profile_payload(payload):
    if isinstance(payload, dict):
        return payload

    if (
        isinstance(payload, list)
        and payload
        and isinstance(payload[0], dict)
        and isinstance(payload[0].get("profile"), dict)
    ):
        return payload[0]["profile"]

    return None


def profile_payload_from_json(raw_json: str):
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        return (
            None,
            f"Invalid JSON syntax: {exc.msg} at line {exc.lineno}, column {exc.colno}.",
        )

    payload = extract_profile_payload(payload)
    if payload is None:
        return (
            None,
            "Valid JSON, but no recognizable company profile object was found. "
            'Paste either a profile object or an n8n response like [{"profile": {...}}].',
        )

    return payload, None


def profile_sections_from_payload(payload: dict):
    profile_sections = {}
    for key in PROFILE_SCHEMA_KEYS:
        if key == "confidence":
            profile_sections[key] = normalize_confidence(payload.get(key))
        else:
            profile_sections[key] = normalize_profile_value(payload.get(key))

    return profile_sections


def profile_sections_from_json(raw_json: str):
    payload, preview_error = profile_payload_from_json(raw_json)
    if preview_error:
        return None, preview_error

    profile_sections = profile_sections_from_payload(payload)
    return profile_sections, None


def first_text(payload: dict, keys: list[str]):
    identifiers = payload.get("identifiers")
    sources = [payload]
    if isinstance(identifiers, dict):
        sources.append(identifiers)

    for source in sources:
        for key in keys:
            value = source.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    return None


def profile_identifiers(payload: dict):
    return {
        "company_name": first_text(payload, ["company_name", "name", "legal_name"]),
        "website_url": first_text(payload, ["website_url", "website", "url"]),
        "cage_code": first_text(payload, ["cage_code", "cage"]),
        "duns": first_text(payload, ["duns", "duns_number"]),
        "uei": first_text(payload, ["uei", "uei_sam"]),
    }


def clean_optional_text(value: str | None):
    if value is None:
        return None
    text = value.strip()
    return text or None


def safe_webhook_host(webhook_url: str | None):
    if not webhook_url:
        return None
    parsed = urlparse(webhook_url)
    return parsed.netloc or parsed.path or "configured webhook"


def company_profile_webhook_url():
    load_dotenv(DOTENV_PATH, override=True)
    return os.getenv("COMPANY_PROFILE_WEBHOOK_URL")


def response_body_snippet(response, limit: int = 300):
    if response is None:
        return None
    try:
        body = response.text
    except Exception:
        return None
    body = " ".join((body or "").split())
    if not body:
        return None
    return body[:limit]


def profile_display(profile: CompanyProfile):
    payload = profile.profile_json if isinstance(profile.profile_json, dict) else {}
    identifiers = profile_identifiers(payload)
    company_name = (
        profile.company_name
        or identifiers["company_name"]
        or profile.website_url
        or identifiers["website_url"]
        or f"Company profile #{profile.id}"
    )

    confidence = normalize_confidence(payload.get("confidence"))

    return {
        "company_name": company_name,
        "website_url": profile.website_url or identifiers["website_url"],
        "cage_code": profile.cage_code or identifiers["cage_code"],
        "duns": profile.duns or identifiers["duns"],
        "uei": profile.uei or identifiers["uei"],
        "confidence": confidence,
        "updated_date": profile.updated_at.strftime("%Y-%m-%d") if profile.updated_at else "Unknown date",
    }


def saved_company_profiles(db: Session, user):
    profiles = (
        db.query(CompanyProfile)
        .filter(CompanyProfile.org_id == _user_org_id(user))
        .order_by(CompanyProfile.updated_at.desc(), CompanyProfile.id.desc())
        .all()
    )
    for profile in profiles:
        profile.display = profile_display(profile)
    return profiles


def latest_company_profile(db: Session, user):
    profile = (
        db.query(CompanyProfile)
        .filter(CompanyProfile.org_id == _user_org_id(user))
        .order_by(CompanyProfile.updated_at.desc(), CompanyProfile.id.desc())
        .first()
    )
    if profile:
        profile.display = profile_display(profile)
    return profile


def find_existing_company_profile(db: Session, user, identifiers: dict):
    matchers = []
    if identifiers["uei"]:
        matchers.append(CompanyProfile.uei == identifiers["uei"])
    if identifiers["duns"]:
        matchers.append(CompanyProfile.duns == identifiers["duns"])
    if identifiers["company_name"]:
        matchers.append(CompanyProfile.company_name == identifiers["company_name"])

    if not matchers:
        return None

    return (
        db.query(CompanyProfile)
        .filter(CompanyProfile.org_id == _user_org_id(user))
        .filter(or_(*matchers))
        .order_by(CompanyProfile.updated_at.desc(), CompanyProfile.id.desc())
        .first()
    )


def render_company_profile(
    request: Request,
    db: Session,
    user,
    profile_sections,
    profile_json: str = "",
    preview_error: str | None = None,
    parsed_profile_json: str = "",
    can_save_profile: bool = False,
    saved_profile: CompanyProfile | None = None,
    generation_message: str | None = None,
    generation_error: str | None = None,
    generation_form: dict | None = None,
):
    if saved_profile:
        saved_profile.display = profile_display(saved_profile)

    return templates.TemplateResponse(
        "company_profile.html",
        {
            "request": request,
            "user": user,
            "active_page": "company_profile",
            "sidebar": get_sidebar(db, user),
            "profile_sections": profile_sections,
            "profile_json": profile_json,
            "preview_error": preview_error,
            "parsed_profile_json": parsed_profile_json,
            "can_save_profile": can_save_profile,
            "saved_profile": saved_profile,
            "saved_profiles": saved_company_profiles(db, user),
            "schema_keys": PROFILE_SCHEMA_KEYS,
            "generation_message": generation_message,
            "generation_error": generation_error,
            "generation_form": generation_form or {},
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

    profile = latest_company_profile(db, user)
    if profile:
        profile_sections = profile_sections_from_payload(profile.profile_json)
        profile_json = json.dumps(profile.profile_json, indent=2)
        return render_company_profile(
            request,
            db,
            user,
            profile_sections,
            profile_json=profile_json,
            parsed_profile_json=profile_json,
            can_save_profile=True,
            saved_profile=profile,
        )

    return render_company_profile(
        request,
        db,
        user,
        get_default_profile_sections(),
    )


@router.post("/company-profile")
async def company_profile_preview(
    request: Request,
    profile_json: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    profile_sections = get_default_profile_sections()
    preview_error = None
    parsed_profile_json = ""
    can_save_profile = False

    if profile_json.strip():
        profile_payload, preview_error = profile_payload_from_json(profile_json)
        if profile_payload is not None:
            profile_sections = profile_sections_from_payload(profile_payload)
            parsed_profile_json = json.dumps(profile_payload, indent=2)
            can_save_profile = True

    return render_company_profile(
        request,
        db,
        user,
        profile_sections,
        profile_json=profile_json,
        preview_error=preview_error,
        parsed_profile_json=parsed_profile_json,
        can_save_profile=can_save_profile,
    )


@router.post("/company-profile/save")
async def company_profile_save(
    request: Request,
    profile_json: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    profile_payload, preview_error = profile_payload_from_json(profile_json)
    if preview_error:
        return render_company_profile(
            request,
            db,
            user,
            get_default_profile_sections(),
            profile_json=profile_json,
            preview_error=preview_error,
        )

    identifiers = profile_identifiers(profile_payload)
    profile = find_existing_company_profile(db, user, identifiers)
    if not profile:
        profile = CompanyProfile(org_id=_user_org_id(user))
        db.add(profile)

    profile.company_name = identifiers["company_name"]
    profile.website_url = identifiers["website_url"]
    profile.cage_code = identifiers["cage_code"]
    profile.duns = identifiers["duns"]
    profile.uei = identifiers["uei"]
    profile.profile_json = profile_payload

    db.commit()
    db.refresh(profile)

    return RedirectResponse(url=f"/company-profile/{profile.id}", status_code=303)


@router.post("/company-profile/generate")
async def company_profile_generate(
    request: Request,
    company_name: str = Form(""),
    website_url: str = Form(""),
    cage_code: str = Form(""),
    duns: str = Form(""),
    uei: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    generation_form = {
        "company_name": company_name,
        "website_url": website_url,
        "cage_code": cage_code,
        "duns": duns,
        "uei": uei,
    }
    webhook_url = company_profile_webhook_url()
    webhook_host = safe_webhook_host(webhook_url)
    logger.info(
        "Company profile webhook config present=%s host=%s dotenv_path=%s user_id=%s",
        bool(webhook_url),
        webhook_host,
        DOTENV_PATH,
        getattr(user, "id", None),
    )
    profile = latest_company_profile(db, user)
    profile_sections = profile_sections_from_payload(profile.profile_json) if profile else get_default_profile_sections()
    profile_json = json.dumps(profile.profile_json, indent=2) if profile else ""

    if not webhook_url:
        logger.warning(
            "Company profile generation requested but COMPANY_PROFILE_WEBHOOK_URL is not configured present=%s host=%s dotenv_path=%s user_id=%s",
            False,
            webhook_host,
            DOTENV_PATH,
            getattr(user, "id", None),
        )
        return render_company_profile(
            request,
            db,
            user,
            profile_sections,
            profile_json=profile_json,
            parsed_profile_json=profile_json,
            can_save_profile=bool(profile),
            saved_profile=profile,
            generation_error=f"COMPANY_PROFILE_WEBHOOK_URL is not configured. Checked {DOTENV_PATH}.",
            generation_form=generation_form,
        )

    payload = {
        "company_name": clean_optional_text(company_name),
        "website_url": clean_optional_text(website_url),
        "cage_code": clean_optional_text(cage_code),
        "duns": clean_optional_text(duns),
        "uei": clean_optional_text(uei),
    }

    try:
        logger.info(
            "Calling company profile webhook host=%s user_id=%s company_name=%s",
            webhook_host,
            getattr(user, "id", None),
            payload["company_name"],
        )
        response = requests.post(webhook_url, json=payload, timeout=120)
        response_snippet = response_body_snippet(response)
        logger.info(
            "Company profile webhook returned host=%s status_code=%s user_id=%s",
            webhook_host,
            response.status_code,
            getattr(user, "id", None),
        )
        if not 200 <= response.status_code < 300:
            logger.warning(
                "Company profile webhook non-2xx host=%s status_code=%s user_id=%s response_body_snippet=%s",
                webhook_host,
                response.status_code,
                getattr(user, "id", None),
                response_snippet,
            )
            return render_company_profile(
                request,
                db,
                user,
                profile_sections,
                profile_json=profile_json,
                parsed_profile_json=profile_json,
                can_save_profile=bool(profile),
                saved_profile=profile,
                generation_error=(
                    "Company profile generation failed: "
                    f"webhook host {webhook_host} returned HTTP {response.status_code}."
                    + (f" Response: {response_snippet}" if response_snippet else "")
                ),
                generation_form=generation_form,
            )
    except requests.RequestException as exc:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        response_snippet = response_body_snippet(response)
        logger.warning(
            "Company profile webhook request failed host=%s status_code=%s user_id=%s error=%s response_body_snippet=%s",
            webhook_host,
            status,
            getattr(user, "id", None),
            exc.__class__.__name__,
            response_snippet,
        )
        status_detail = f" HTTP {status}." if status else ""
        return render_company_profile(
            request,
            db,
            user,
            profile_sections,
            profile_json=profile_json,
            parsed_profile_json=profile_json,
            can_save_profile=bool(profile),
            saved_profile=profile,
            generation_error=(
                "Company profile generation failed: "
                f"webhook host {webhook_host} did not return a successful response.{status_detail}"
                + (f" Response: {response_snippet}" if response_snippet else "")
            ),
            generation_form=generation_form,
        )

    db.expire_all()
    identifiers = {
        "company_name": payload["company_name"],
        "website_url": payload["website_url"],
        "cage_code": payload["cage_code"],
        "duns": payload["duns"],
        "uei": payload["uei"],
    }
    profile = find_existing_company_profile(db, user, identifiers) or latest_company_profile(db, user)
    profile_sections = profile_sections_from_payload(profile.profile_json) if profile else get_default_profile_sections()
    profile_json = json.dumps(profile.profile_json, indent=2) if profile else ""

    return render_company_profile(
        request,
        db,
        user,
        profile_sections,
        profile_json=profile_json,
        parsed_profile_json=profile_json,
        can_save_profile=bool(profile),
        saved_profile=profile,
        generation_message=(
            "Company profile generation completed: "
            f"webhook host {webhook_host} returned HTTP {response.status_code}. "
            "Saved profiles have been refreshed."
        ),
        generation_form={},
    )


@router.get("/company-profile/{profile_id}")
async def company_profile_saved_detail(
    profile_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    profile = (
        db.query(CompanyProfile)
        .filter(
            CompanyProfile.id == profile_id,
            CompanyProfile.org_id == _user_org_id(user),
        )
        .first()
    )
    if not profile:
        return RedirectResponse(url="/company-profile", status_code=303)

    profile_sections = profile_sections_from_payload(profile.profile_json)
    profile_json = json.dumps(profile.profile_json, indent=2)

    return render_company_profile(
        request,
        db,
        user,
        profile_sections,
        profile_json=profile_json,
        parsed_profile_json=profile_json,
        can_save_profile=True,
        saved_profile=profile,
    )
