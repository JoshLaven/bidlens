from datetime import date, datetime
from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from collections import OrderedDict
from ..database import get_db
from ..models import Opportunity, User, UserOpportunity, OpportunityStatus
from ..auth import get_current_user
from sqlalchemy import and_, or_
from sqlalchemy.orm import aliased
from ..models import OpportunityState
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from sqlalchemy import func, case
from ..models import Vote

router = APIRouter()
templates = Jinja2Templates(directory="src/bidlens/templates")

def require_user(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None
    return user

@dataclass
class SavedItemVM:
    opportunity: object
    notes: Optional[str] = None
    internal_deadline: Optional[datetime] = None
    created_at: Optional[datetime] = None

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

    # Base query by tab
    if tab == "solicitations":
        query = db.query(Opportunity).filter(Opportunity.opportunity_type.in_(solicitation_types))
    else:
        query = db.query(Opportunity).filter(Opportunity.opportunity_type.in_(rfi_types))

    # NEW: Feed truth comes from OpportunityState (org-level)
    OS = aliased(OpportunityState)

    query = query.outerjoin(
        OS,
        and_(
            OS.opp_id == Opportunity.id,
            OS.org_id == user.organization_id,
        )
    )

    # If NOT show_all: show only FEED (or no state row yet)
    if not show_all:
        query = query.filter(or_(OS.id.is_(None), OS.state == "FEED"))

    query = query.order_by(Opportunity.response_deadline.asc())

    total_count = query.count()
    limit = None if show_all else 20
    opportunities = query.limit(limit).all() if limit else query.all()

    # Optional: keep your existing per-user status badges for now (notes/etc)
    # This does NOT drive filtering anymore.
    user_opps = {
        uo.opportunity_id: uo.status
        for uo in db.query(UserOpportunity).filter(UserOpportunity.user_id == user.id).all()
    }

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

    # user metadata (notes/internal deadline) stays per-user
    user_opp = db.query(UserOpportunity).filter(
        UserOpportunity.user_id == user.id,
        UserOpportunity.opportunity_id == opp_id
    ).first()

    # org-level decision state (truth)
    state_row = db.query(OpportunityState).filter(
        and_(
            OpportunityState.org_id == user.organization_id,
            OpportunityState.opp_id == opp_id
        )
    ).first()
    decision_state = state_row.state if state_row else "FEED"   # "FEED" | "SAVED" | "BID" | "NO_BID"

    today = date.today()
    days_until_due = (opportunity.response_deadline - today).days
    
    # --- Votes (org-level counts + user's vote) ---
    vote_counts = db.query(
        func.coalesce(func.sum(case((Vote.vote == "UP", 1), else_=0)), 0).label("up"),
        func.coalesce(func.sum(case((Vote.vote == "DOWN", 1), else_=0)), 0).label("down"),
        func.coalesce(func.sum(case((Vote.vote == "PASS", 1), else_=0)), 0).label("pass_"),
    ).filter(
        Vote.org_id == user.organization_id,
        Vote.opp_id == opp_id
    ).first()

    print(
        "PASS COUNT (raw):",
        db.query(Vote)
          .filter(Vote.opp_id == opp_id, Vote.vote == "PASS")
          .count()
    )
    my_vote_row = db.query(Vote).filter(
        Vote.org_id == user.organization_id,
        Vote.opp_id == opp_id,
        Vote.user_id == user.id
    ).first()

    my_vote = my_vote_row.vote if my_vote_row else None  # "UP" | "DOWN" | "PASS" | None

    up_count = int(vote_counts.up) if vote_counts else 0
    down_count = int(vote_counts.down) if vote_counts else 0
    pass_count = int(vote_counts.pass_) if vote_counts else 0

    return templates.TemplateResponse("detail.html", {
        "request": request,
        "user": user,
        "opportunity": opportunity,
        "user_opp": user_opp,
        "decision_state": decision_state,
        "days_until_due": days_until_due,
        "active_page": None,
        "up_count": up_count,
        "down_count": down_count,
        "pass_count": pass_count,
        "my_vote": my_vote,
        "show_votes": True
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
    
@router.post("/opportunity/{opp_id}/unskip")
async def unskip_opportunity(
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

    # "Unskip" = revert to no user-specific status by removing the row
    if user_opp:
        db.delete(user_opp)
        db.commit()

    referer = request.headers.get("referer", "/saved")
    return RedirectResponse(url=referer, status_code=303)

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

    # UPSERT: create row if missing so user can always take notes
    if not user_opp:
        user_opp = UserOpportunity(
            user_id=user.id,
            opportunity_id=opp_id
        )
        db.add(user_opp)
        db.flush()  # ensures it has an id before we commit (safe)

    # Internal deadline
    if internal_deadline:
        user_opp.internal_deadline = datetime.strptime(internal_deadline, "%Y-%m-%d").date()
    else:
        user_opp.internal_deadline = None

    # Notes
    user_opp.notes = notes if notes else None

    db.commit()

    return RedirectResponse(url=f"/opportunity/{opp_id}", status_code=303)


@router.get("/saved")
async def saved_page(
    request: Request,
    state: str | None = "saved",   # saved | bid | no_bid
    type: str | None = None,
    db: Session = Depends(get_db)
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    # Map URL state → canonical OpportunityState
    STATE_MAP = {
        "saved": "SAVED",
        "bid": "BID",
        "no_bid": "NO_BID",
    }
    state_value = STATE_MAP.get(state, "SAVED")

    # Base query: decision truth from OpportunityState
    query = (
        db.query(Opportunity, UserOpportunity)
        .join(
            OpportunityState,
            and_(
                OpportunityState.opp_id == Opportunity.id,
                OpportunityState.org_id == user.organization_id,
                OpportunityState.state == state_value,
            ),
        )
        .outerjoin(
            UserOpportunity,
            and_(
                UserOpportunity.opportunity_id == Opportunity.id,
                UserOpportunity.user_id == user.id,
            ),
        )
    )

    # Optional type filter
    if type == "solicitation":
        solicitation_types = ["Solicitation", "Combined Synopsis/Solicitation", "Award Notice"]
        query = query.filter(Opportunity.opportunity_type.in_(solicitation_types))
    elif type == "rfi":
        rfi_types = ["RFI", "Sources Sought", "Special Notice", "Pre-Solicitation"]
        query = query.filter(Opportunity.opportunity_type.in_(rfi_types))

    rows = query.order_by(Opportunity.response_deadline.asc()).all()

    # Shape results to match template expectations:
    # item.opportunity, item.notes, item.internal_deadline, item.created_at
    saved_items = []
    for opp, user_opp in rows:
        saved_items.append(
            SavedItemVM(
                opportunity=opp,
                notes=getattr(user_opp, "notes", None) if user_opp else None,
                internal_deadline=getattr(user_opp, "internal_deadline", None) if user_opp else None,
                created_at=getattr(user_opp, "created_at", None) if user_opp else None,
            )
        )

    return templates.TemplateResponse("saved.html", {
        "request": request,
        "user": user,
        "saved_items": saved_items,
        "state_filter": state,   # ← used by tabs + template logic
        "type_filter": type,
        "active_page": "saved",
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
