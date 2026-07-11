from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..auth import attach_request_user_context, get_current_user
from ..models import OrgProfile, OrganizationMembership

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


def _org_query(request: Request) -> str:
    query = str(request.url.query or "").strip()
    return f"?{query}" if query else ""


def _workspace_management_sections(request: Request, organization_id: int) -> list[dict[str, str]]:
    org_q = _org_query(request)
    member_q = org_q or f"?org_id={organization_id}"
    return [
        {
            "key": "organization",
            "label": "Organization",
            "description": "Manage the organization profile, website, government identifiers, and recent work context.",
            "url": f"/company-profile{org_q}",
            "cta": "Open Organization",
        },
        {
            "key": "members",
            "label": "Workspace Members",
            "description": "Invite teammates, review pending invitations, and manage active workspace membership.",
            "url": f"/admin/organizations/{organization_id}/users{member_q}",
            "cta": "Manage Members",
        },
        {
            "key": "discovery",
            "label": "Opportunity Discovery",
            "description": "Configure inbound sources such as SAM.gov, Grants.gov, and GovWin.",
            "url": f"/connect-sources{org_q}",
            "cta": "Manage Discovery",
        },
        {
            "key": "business-systems",
            "label": "Business Systems",
            "description": "Manage outbound systems such as Salesforce and future CRM destinations.",
            "url": f"/outbound-integrations{org_q}",
            "cta": "Manage Systems",
        },
        {
            "key": "lanes",
            "label": "Pursuit Lanes",
            "description": "Manage the shared lane taxonomy BidLens uses to organize opportunities.",
            "url": f"/pursuit-lanes{org_q}",
            "cta": "Manage Lanes",
        },
        {
            "key": "feed-rules",
            "label": "Feed Rules",
            "description": "Configure workspace defaults, triage mode, and digest settings.",
            "url": f"/settings{org_q}",
            "cta": "Open Feed Rules",
        },
        {
            "key": "import-history",
            "label": "Import History",
            "description": "Review source pulls, manual imports, and operational intake history.",
            "url": f"/imports/history{org_q}",
            "cta": "View History",
        },
        {
            "key": "setup-history",
            "label": "Setup History",
            "description": "Review setup completion from Home until a dedicated workspace history page exists.",
            "url": f"/home{org_q}#setup-history",
            "cta": "View Setup History",
        },
    ]


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

    return templates.TemplateResponse("administration.html", {
        "request": request,
        "user": user,
        "active_page": "administration",
        "sections": _workspace_management_sections(request, _user_org_id(user)),
    })


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

    db.commit()

    return RedirectResponse(url=_settings_redirect_url(request), status_code=303)
