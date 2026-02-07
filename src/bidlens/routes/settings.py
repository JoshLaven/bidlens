from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..auth import get_current_user
from ..models import OrgProfile

router = APIRouter()
templates = Jinja2Templates(directory="src/bidlens/templates")

def require_user(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None
    return user

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
    profile = db.query(OrgProfile).filter(OrgProfile.org_id == user.organization_id).first()

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "user": user,
        "profile": profile,
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
    db: Session = Depends(get_db)
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    profile = db.query(OrgProfile).filter(OrgProfile.org_id == user.organization_id).first()
    if not profile:
        profile = OrgProfile(org_id=user.organization_id)
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

    db.commit()

    return RedirectResponse(url="/settings", status_code=303)
