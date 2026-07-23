from fastapi import APIRouter, HTTPException, Request, Form, Depends
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from ..database import get_db
from ..models import User
from ..auth import create_session, clear_session, is_platform_admin_email
from ..models import Organization
from ..tenancy import (
    ensure_membership,
    normalize_email,
    organization_for_email_domain,
    resolve_user_organization,
)
from ..services.platform import post_authentication_destination_url

router = APIRouter()
templates = Jinja2Templates(directory="src/bidlens/templates")


def _get_or_create_platform_org(db: Session) -> Organization:
    org = db.query(Organization).filter(Organization.slug == "bidlens-platform").first()
    if org:
        return org
    org = Organization(
        name="BidLens Platform",
        slug="bidlens-platform",
        email_domain=None,
        plan="platform",
        is_active=True,
        is_live=True,
    )
    db.add(org)
    db.flush()
    return org

@router.get("/login")
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {
        "request": request,
        "user": None
    })

@router.post("/login")
async def login(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    email = normalize_email(email)
    user = db.query(User).filter(User.email == email).first()

    if is_platform_admin_email(email):
        org = _get_or_create_platform_org(db)
        if not user:
            user = User(email=email, organization_id=org.id)
            db.add(user)
            db.flush()
        else:
            user.organization_id = org.id
        ensure_membership(db, organization_id=org.id, user_id=user.id, role="admin")
        db.commit()
        db.refresh(user)
        response = RedirectResponse(url="/platform", status_code=303)
        create_session(response, user.id)
        return response

    if not user:
        try:
            org = organization_for_email_domain(db, email)
        except HTTPException as exc:
            return templates.TemplateResponse("login.html", {
                "request": request,
                "user": None,
                "email": email,
                "error": exc.detail,
            }, status_code=exc.status_code)
        if not org:
            return templates.TemplateResponse("login.html", {
                "request": request,
                "user": None,
                "email": email,
                "error": "This email is not associated with a provisioned BidLens workspace.",
            }, status_code=403)

        user = User(email=email, organization_id=org.id)
        db.add(user)
        db.flush()
        ensure_membership(db, organization_id=org.id, user_id=user.id, role="member")
        db.commit()
        db.refresh(user)
    else:
        try:
            matched_org = resolve_user_organization(db, user)
        except HTTPException as exc:
            return templates.TemplateResponse("login.html", {
                "request": request,
                "user": None,
                "email": email,
                "error": exc.detail,
            }, status_code=exc.status_code)
        if matched_org and user.organization_id != matched_org.id:
            user.organization_id = matched_org.id
        db.commit()

    
    response = RedirectResponse(
        url=post_authentication_destination_url(db, user),
        status_code=303,
    )
    create_session(response, user.id)
    return response

@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    clear_session(response)
    return response
