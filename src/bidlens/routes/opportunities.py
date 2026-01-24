from datetime import date, datetime
from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from collections import OrderedDict
from ..database import get_db
from ..models import Opportunity, User, UserOpportunity, OpportunityStatus
from ..auth import get_current_user

router = APIRouter()
templates = Jinja2Templates(directory="src/bidlens/templates")

def require_user(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None
    return user

@router.get("/")
async def feed(
    request: Request,
    tab: str = "solicitations",
    show_all: bool = False,
    db: Session = Depends(get_db)
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    
    solicitation_types = ["Solicitation", "Combined Synopsis/Solicitation", "Award Notice"]
    rfi_types = ["RFI", "Sources Sought", "Special Notice", "Pre-Solicitation"]
    
    if tab == "solicitations":
        query = db.query(Opportunity).filter(Opportunity.opportunity_type.in_(solicitation_types))
    else:
        query = db.query(Opportunity).filter(Opportunity.opportunity_type.in_(rfi_types))
    
    query = query.order_by(Opportunity.response_deadline.asc())
    
    total_count = query.count()
    limit = None if show_all else 20
    opportunities = query.limit(limit).all() if limit else query.all()
    
    user_opps = {uo.opportunity_id: uo.status for uo in db.query(UserOpportunity).filter(UserOpportunity.user_id == user.id).all()}
    
    today = date.today()
    for opp in opportunities:
        opp.days_until_due = (opp.response_deadline - today).days
        opp.user_status = user_opps.get(opp.id)
    
    return templates.TemplateResponse("feed.html", {
        "request": request,
        "user": user,
        "opportunities": opportunities,
        "current_tab": tab,
        "show_all": show_all,
        "has_more": total_count > 20,
        "active_page": "feed"
    })

@router.get("/opportunity/{opp_id}")
async def opportunity_detail(
    request: Request,
    opp_id: int,
    db: Session = Depends(get_db)
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    
    opportunity = db.query(Opportunity).filter(Opportunity.id == opp_id).first()
    if not opportunity:
        return RedirectResponse(url="/", status_code=303)
    
    user_opp = db.query(UserOpportunity).filter(
        UserOpportunity.user_id == user.id,
        UserOpportunity.opportunity_id == opp_id
    ).first()
    
    today = date.today()
    days_until_due = (opportunity.response_deadline - today).days
    
    return templates.TemplateResponse("detail.html", {
        "request": request,
        "user": user,
        "opportunity": opportunity,
        "user_opp": user_opp,
        "days_until_due": days_until_due,
        "active_page": None
    })

@router.post("/opportunity/{opp_id}/save")
async def save_opportunity(
    request: Request,
    opp_id: int,
    db: Session = Depends(get_db)
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    
    existing = db.query(UserOpportunity).filter(
        UserOpportunity.user_id == user.id,
        UserOpportunity.opportunity_id == opp_id
    ).first()
    
    if existing:
        existing.status = OpportunityStatus.SAVED
    else:
        user_opp = UserOpportunity(
            user_id=user.id,
            opportunity_id=opp_id,
            status=OpportunityStatus.SAVED
        )
        db.add(user_opp)
    
    db.commit()
    
    referer = request.headers.get("referer", "/")
    return RedirectResponse(url=referer, status_code=303)

#@router.post("/opportunity/{opp_id}/unsave")
async def unsave_opportunity(
    request: Request,
    opp_id: int,
    db: Session = Depends(get_db)
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    
    user_opp = db.query(UserOpportunity).filter(
        UserOpportunity.user_id == user.id,
        UserOpportunity.opportunity_id == opp_id
    ).first()
    
    if user_opp:
        db.delete(user_opp)
        db.commit()
    
    return RedirectResponse(url="/", status_code=303)

@router.post("/opportunity/{opp_id}/drop")
async def drop_opportunity(
    request: Request,
    opp_id: int,
    db: Session = Depends(get_db)
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    
    user_opp = db.query(UserOpportunity).filter(
        UserOpportunity.user_id == user.id,
        UserOpportunity.opportunity_id == opp_id
    ).first()
    
    if user_opp:
        user_opp.status = OpportunityStatus.DROPPED
        db.commit()
    else:
        user_opp = UserOpportunity(
            user_id=user.id,
            opportunity_id=opp_id,
            status=OpportunityStatus.DROPPED
        )
        db.add(user_opp)
        db.commit()
    
    referer = request.headers.get("referer", "/")
    return RedirectResponse(url=referer, status_code=303)

@router.post("/opportunity/{opp_id}/status")
async def update_status(
    request: Request,
    opp_id: int,
    status: str = Form(...),
    db: Session = Depends(get_db)
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    
    user_opp = db.query(UserOpportunity).filter(
        UserOpportunity.user_id == user.id,
        UserOpportunity.opportunity_id == opp_id
    ).first()
    
    if user_opp and status in [s.value for s in OpportunityStatus]:
        user_opp.status = status
        db.commit()
    
    return RedirectResponse(url=f"/opportunity/{opp_id}", status_code=303)

@router.post("/opportunity/{opp_id}/update")
async def update_opportunity(
    request: Request,
    opp_id: int,
    internal_deadline: str = Form(None),
    notes: str = Form(None),
    db: Session = Depends(get_db)
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    
    user_opp = db.query(UserOpportunity).filter(
        UserOpportunity.user_id == user.id,
        UserOpportunity.opportunity_id == opp_id
    ).first()
    
    if user_opp:
        if internal_deadline:
            user_opp.internal_deadline = datetime.strptime(internal_deadline, "%Y-%m-%d").date()
        else:
            user_opp.internal_deadline = None
        user_opp.notes = notes if notes else None
        db.commit()
    
    return RedirectResponse(url=f"/opportunity/{opp_id}", status_code=303)

@router.get("/saved")
async def saved_page(
    request: Request,
    status: str | None = None,
    type: str | None = None,
    db: Session = Depends(get_db)
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    
    query = db.query(UserOpportunity).filter(UserOpportunity.user_id == user.id)
    
    if status:
        query = query.filter(UserOpportunity.status == status)
    
    saved_items = query.all()
    
    if type == "solicitation":
        solicitation_types = ["Solicitation", "Combined Synopsis/Solicitation", "Award Notice"]
        saved_items = [item for item in saved_items if item.opportunity.opportunity_type in solicitation_types]
    elif type == "rfi":
        rfi_types = ["RFI", "Sources Sought", "Special Notice", "Pre-Solicitation"]
        saved_items = [item for item in saved_items if item.opportunity.opportunity_type in rfi_types]
    
    return templates.TemplateResponse("saved.html", {
        "request": request,
        "user": user,
        "saved_items": saved_items,
        "status_filter": status,
        "type_filter": type,
        "active_page": "saved"
    })

@router.get("/calendar")
async def calendar_page(
    request: Request,
    db: Session = Depends(get_db)
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    
    solicitation_types = ["Solicitation", "Combined Synopsis/Solicitation", "Award Notice"]
    
    saved_items = db.query(UserOpportunity).filter(
        UserOpportunity.user_id == user.id,
        UserOpportunity.status.in_([OpportunityStatus.SAVED, OpportunityStatus.IN_PROGRESS])
    ).all()
    
    saved_items = [item for item in saved_items if item.opportunity.opportunity_type in solicitation_types]
    
    for item in saved_items:
        item.display_date = item.internal_deadline or item.opportunity.response_deadline
    
    saved_items.sort(key=lambda x: x.display_date)
    
    months = OrderedDict()
    for item in saved_items:
        month_key = item.display_date.strftime("%B %Y")
        week_num = (item.display_date.day - 1) // 7 + 1
        week_key = f"Week {week_num}"
        
        if month_key not in months:
            months[month_key] = OrderedDict()
        if week_key not in months[month_key]:
            months[month_key][week_key] = []
        
        months[month_key][week_key].append(item)
    
    return templates.TemplateResponse("calendar.html", {
        "request": request,
        "user": user,
        "months": months,
        "active_page": "calendar"
    })
