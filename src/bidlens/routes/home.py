from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..auth import attach_request_user_context, get_current_user, is_platform_admin_email
from ..database import get_db
from ..models import Event, Organization
from ..services.home import get_daily_brief_home_context, get_home_context
from ..services.platform import post_authentication_destination_url


router = APIRouter()
templates = Jinja2Templates(directory="src/bidlens/templates")


@router.get("/home")
async def home_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if is_platform_admin_email(user.email):
        return RedirectResponse(url="/platform", status_code=303)

    attach_request_user_context(request, db, user)
    destination_url = post_authentication_destination_url(
        db,
        user,
        organization_id=user.current_organization_id,
    )
    if destination_url.startswith("/organization-setup"):
        return RedirectResponse(url=destination_url, status_code=303)

    context = get_daily_brief_home_context(
        db,
        organization_id=user.current_organization_id,
        user_id=user.id,
    )
    return templates.TemplateResponse("home.html", {
        "request": request,
        "user": user,
        "active_page": "home",
        "home": context,
    })


@router.get("/organization-setup")
async def organization_setup_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if is_platform_admin_email(user.email):
        return RedirectResponse(url="/platform", status_code=303)

    attach_request_user_context(request, db, user)
    if getattr(user, "current_role", "member") != "admin":
        return RedirectResponse(url="/home", status_code=303)

    context = get_home_context(
        db,
        organization_id=user.current_organization_id,
        user_id=user.id,
    )
    if context["is_live"]:
        return RedirectResponse(
            url=f"/home?org_id={user.current_organization_id}",
            status_code=303,
        )

    return templates.TemplateResponse("organization_setup.html", {
        "request": request,
        "user": user,
        "active_page": "home",
        "home": context,
    })


@router.post("/home/go-live")
async def go_live(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    attach_request_user_context(request, db, user)
    if getattr(user, "current_role", "member") != "admin":
        return RedirectResponse(url="/", status_code=303)

    context = get_home_context(
        db,
        organization_id=user.current_organization_id,
        user_id=user.id,
    )
    if not context["can_go_live"]:
        return RedirectResponse(url="/home", status_code=303)

    organization = (
        db.query(Organization)
        .filter(Organization.id == user.current_organization_id)
        .first()
    )
    if organization is None:
        return RedirectResponse(url="/home", status_code=303)

    organization.is_live = True
    db.add(Event(
        org_id=organization.id,
        user_id=user.id,
        opp_id=None,
        event_type="workspace_went_live",
        ui_version="v1",
        payload={"source": "home_go_live"},
    ))
    db.commit()
    return RedirectResponse(url="/home", status_code=303)
