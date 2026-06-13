from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from ..database import get_db
from ..models import User
from ..auth import create_session, clear_session
from ..models import Organization
from ..tenancy import (
    email_domain,
    ensure_email_domain_membership,
    ensure_membership,
    normalize_email,
    normalize_org_email_domain,
    organization_for_email_domain,
    unique_org_slug,
)

router = APIRouter()
templates = Jinja2Templates(directory="src/bidlens/templates")

@router.get("/login")
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {
        "request": request,
        "user": None
    })

@router.post("/login")
async def login(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    def org_name_for_email(email: str) -> str:
        domain = email_domain(email)
        if not domain or normalize_org_email_domain(domain) is None:
            return f"{email}'s Org"
        return domain

    email = normalize_email(email)
    domain = email_domain(email)
    user = db.query(User).filter(User.email == email).first()

    if not user:
        org = organization_for_email_domain(db, email)
        role = "member"
        if not org:
            org = Organization(
                name=org_name_for_email(email),
                slug=unique_org_slug(db, org_name_for_email(email)),
                email_domain=normalize_org_email_domain(domain),
                plan="free",
                is_active=True
            )
            db.add(org)
            db.flush()  # assigns org.id without needing a commit yet
            role = "admin"

        user = User(email=email, organization_id=org.id)
        db.add(user)
        db.flush()
        ensure_membership(db, organization_id=org.id, user_id=user.id, role=role)
        db.commit()
        db.refresh(user)
    else:
        matched_org = ensure_email_domain_membership(db, user)
        # Safety: if existing user predates orgs, attach them
        if not getattr(user, "organization_id", None):
            if matched_org:
                user.organization_id = matched_org.id
            else:
                org = Organization(
                    name=org_name_for_email(user.email),
                    slug=unique_org_slug(db, org_name_for_email(user.email)),
                    email_domain=normalize_org_email_domain(email_domain(user.email)),
                    plan="free",
                    is_active=True
                )
                db.add(org)
                db.flush()
                user.organization_id = org.id
        ensure_membership(db, organization_id=user.organization_id, user_id=user.id, role="admin")
        db.commit()

    
    response = RedirectResponse(url="/", status_code=303)
    create_session(response, user.id)
    return response

@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    clear_session(response)
    return response
