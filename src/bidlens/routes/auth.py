from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from ..database import get_db
from ..models import User
from ..auth import create_session, clear_session
from ..models import Organization

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
        domain = email.split("@")[-1].lower().strip()
        if domain in {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com"}:
            return f"{email}'s Org"
        return domain

    user = db.query(User).filter(User.email == email).first()

    if not user:
        org = Organization(
            name=org_name_for_email(email),
            plan="free",
            is_active=True
        )
        db.add(org)
        db.flush()  # assigns org.id without needing a commit yet

        user = User(email=email, organization_id=org.id)
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        # Safety: if existing user predates orgs, attach them
        if not getattr(user, "organization_id", None):
            org = Organization(
                name=org_name_for_email(user.email),
                plan="free",
                is_active=True
            )
            db.add(org)
            db.flush()
            user.organization_id = org.id
            db.commit()

    
    response = RedirectResponse(url="/", status_code=303)
    create_session(response, user.id)
    return response

@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    clear_session(response)
    return response
