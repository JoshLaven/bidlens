from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from ..database import get_db
from ..models import User
from ..auth import create_session, clear_session

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
    email = email.lower().strip()
    user = db.query(User).filter(User.email == email).first()
    if not user:
        user = User(email=email)
        db.add(user)
        db.commit()
        db.refresh(user)
    
    response = RedirectResponse(url="/", status_code=303)
    create_session(response, user.id)
    return response

@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    clear_session(response)
    return response
