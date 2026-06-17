from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..services.govwin_import import import_govwin_xlsx
from ..tenancy import current_org_id
from .opportunities import get_sidebar

router = APIRouter()
templates = Jinja2Templates(directory="src/bidlens/templates")


def require_user(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None
    setattr(user, "current_organization_id", current_org_id(request, db, user))
    return user


def _user_org_id(user) -> int:
    return getattr(user, "current_organization_id", None) or user.organization_id


def _context(request: Request, user, result=None, error: str | None = None):
    return {
        "request": request,
        "user": user,
        "result": result,
        "error": error,
        "active_page": "imports",
    }


@router.get("/imports/govwin")
async def govwin_import_page(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    context = _context(request, user)
    context["sidebar"] = get_sidebar(db, user)
    return templates.TemplateResponse("govwin_import.html", context)


@router.post("/imports/govwin")
async def govwin_import_upload(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    error = None
    result = None
    filename = file.filename or ""
    if not filename.lower().endswith(".xlsx"):
        error = "Upload a GovWin .xlsx export."
    else:
        try:
            file_bytes = await file.read()
            if not file_bytes:
                error = "The uploaded file was empty."
            else:
                result = import_govwin_xlsx(db, _user_org_id(user), file_bytes)
                db.commit()
        except Exception as exc:
            db.rollback()
            error = f"Unable to import GovWin export: {exc}"

    context = _context(request, user, result=result, error=error)
    context["sidebar"] = get_sidebar(db, user)
    return templates.TemplateResponse("govwin_import.html", context)
