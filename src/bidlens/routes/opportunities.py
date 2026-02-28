from datetime import date, datetime, timedelta
from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from collections import OrderedDict
from ..database import get_db
from ..models import Opportunity, User, UserOpportunity, OpportunityStatus, OrgProfile
from ..auth import get_current_user
from sqlalchemy import and_, or_
from sqlalchemy.orm import aliased
from ..models import OpportunityState
from dataclasses import dataclass
from typing import Optional
from sqlalchemy import func, case
from ..models import Vote
from ..models import OpportunityBrief


router = APIRouter()
templates = Jinja2Templates(directory="src/bidlens/templates")
CLOSED_STATES = ("BID", "NO_BID")


def _parse_csv(text: str | None) -> list[str]:
    """Split comma-separated OrgProfile field into a cleaned list."""
    if not text:
        return []
    return [s.strip() for s in text.split(",") if s.strip()]


def apply_org_filters(query, db: Session, user):
    """Apply OrgProfile keyword/agency/deadline filters to an Opportunity query."""
    profile = db.query(OrgProfile).filter(OrgProfile.org_id == user.organization_id).first()
    if not profile:
        return query

    # --- Keyword filters (match against title, case-insensitive) ---
    include_kw = _parse_csv(profile.include_keywords)
    exclude_kw = _parse_csv(profile.exclude_keywords)

    if include_kw:
        # Show only opps where title matches at least one include keyword
        conditions = [Opportunity.title.ilike(f"%{kw}%") for kw in include_kw]
        query = query.filter(or_(*conditions))

    if exclude_kw:
        # Hide opps where title matches any exclude keyword
        for kw in exclude_kw:
            query = query.filter(~Opportunity.title.ilike(f"%{kw}%"))

    # --- Agency filters ---
    include_ag = _parse_csv(profile.include_agencies)
    exclude_ag = _parse_csv(profile.exclude_agencies)

    if include_ag:
        conditions = [Opportunity.agency.ilike(f"%{ag}%") for ag in include_ag]
        query = query.filter(or_(*conditions))

    if exclude_ag:
        for ag in exclude_ag:
            query = query.filter(~Opportunity.agency.ilike(f"%{ag}%"))

    # --- Deadline window (days out from today) ---
    today = date.today()
    if profile.min_days_out is not None:
        query = query.filter(Opportunity.response_deadline >= today + timedelta(days=profile.min_days_out))
    if profile.max_days_out is not None:
        query = query.filter(Opportunity.response_deadline <= today + timedelta(days=profile.max_days_out))

    return query

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
    up_count: int = 0
    down_count: int = 0
    pass_count: int = 0



@router.get("/")
async def feed(
    request: Request,
    tab: str = "solicitations",
    show_all: bool = False,  # you can ignore this later if you want
    db: Session = Depends(get_db)
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    solicitation_types = ["Solicitation", "Combined Synopsis/Solicitation", "Award Notice"]
    rfi_types = ["RFI", "Sources Sought", "Special Notice", "Pre-Solicitation"]

    OS = aliased(OpportunityState)
    MyVote = aliased(Vote)

    base = db.query(
        Opportunity,

        # Count org signals (UP = Shortlist, DOWN = Pass)
        func.coalesce(func.sum(case((Vote.vote == "UP", 1), else_=0)), 0).label("shortlist_count"),
        func.coalesce(func.sum(case((Vote.vote == "DOWN", 1), else_=0)), 0).label("pass_count"),

        # My personal signal
        MyVote.vote.label("my_signal"),

        UserOpportunity.watched.label("watched"),
    ).outerjoin(
        OS,
        and_(OS.opp_id == Opportunity.id, OS.org_id == user.organization_id)
    ).outerjoin(
        Vote,
        and_(Vote.opp_id == Opportunity.id, Vote.org_id == user.organization_id)
    ).outerjoin(
        MyVote,
        and_(MyVote.opp_id == Opportunity.id, MyVote.org_id == user.organization_id, MyVote.user_id == user.id)
    ).outerjoin(
        UserOpportunity,
        and_(UserOpportunity.opportunity_id == Opportunity.id, UserOpportunity.user_id == user.id)
    )

    # ✅ REMOVE these “scatter” filters for the reset:
    # .filter(or_(OS.id.is_(None), OS.state.notin_(CLOSED_STATES)))
    # and the “hide my passed unless show_all”
    # Option A: hide items I personally PASSED unless show_all=1
    #if not show_all:
    #    base = base.filter(or_(MyVote.vote.is_(None), MyVote.vote != "PASS"))

    base = base.group_by(Opportunity.id, MyVote.vote, UserOpportunity.watched)

    # Type tab filter
    if tab == "solicitations":
        base = base.filter(Opportunity.opportunity_type.in_(solicitation_types))
    else:
        base = base.filter(Opportunity.opportunity_type.in_(rfi_types))

    # Apply org-level keyword/agency/deadline filters from Settings
    base = apply_org_filters(base, db, user)

    rows = base.order_by(Opportunity.response_deadline.asc()).limit(50).all()

    today = date.today()
    opportunities = []
    for opp, shortlist_count, pass_count, my_signal, watched in rows:
        opp.days_until_due = (opp.response_deadline - today).days
        opp.pursue_count = int(shortlist_count)
        opp.pass_count = int(pass_count)
        opp.my_signal = my_signal  # "UP" | "PASS" | None
        opp.watched = bool(watched) if watched is not None else False
        opportunities.append(opp)

    return templates.TemplateResponse("feed.html", {
        "request": request,
        "user": user,
        "opportunities": opportunities,
        "current_tab": tab,
        "show_all": show_all,
        "active_page": "feed",
        "sidebar": get_sidebar(db, user)
    })

@router.get("/shortlist")
async def shortlist(
    request: Request,
    tab: str = "solicitations",
    db: Session = Depends(get_db)
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    solicitation_types = ["Solicitation", "Combined Synopsis/Solicitation", "Award Notice"]
    rfi_types = ["RFI", "Sources Sought", "Special Notice", "Pre-Solicitation"]

    OS = aliased(OpportunityState)

    # My vote row (force match)
    MyVote = aliased(Vote)

    # Org counts (separate alias so the sum doesn't get constrained by MyVote join)
    OrgVote = aliased(Vote)

    shortlist_count = func.coalesce(func.sum(case((OrgVote.vote == "UP", 1), else_=0)), 0)
    pass_count = func.coalesce(func.sum(case((OrgVote.vote.in_(["DOWN", "PASS"]), 1), else_=0)), 0)

    q = (
        db.query(
            Opportunity,
            shortlist_count.label("shortlist_count"),
            pass_count.label("pass_count"),
            MyVote.vote.label("my_signal"),
            UserOpportunity.watched.label("watched"),
        )
        # .outerjoin(
        #     OS,
        #     and_(OS.opp_id == Opportunity.id, OS.org_id == user.organization_id)
        # )
        # # optional: hide closed
        # .filter(or_(OS.id.is_(None), OS.state.notin_(CLOSED_STATES)))

        # ✅ THIS is the definition of "Shortlist"
        .join(
            MyVote,
            and_(
                MyVote.opp_id == Opportunity.id,
                MyVote.org_id == user.organization_id,
                MyVote.user_id == user.id,
                MyVote.vote == "UP",
            ),
        )

        .outerjoin(
            OrgVote,
            and_(
                OrgVote.opp_id == Opportunity.id,
                OrgVote.org_id == user.organization_id,
            ),
        )
        .outerjoin(
            UserOpportunity,
            and_(
                UserOpportunity.opportunity_id == Opportunity.id,
                UserOpportunity.user_id == user.id,
            ),
        )
        .group_by(Opportunity.id, MyVote.vote, UserOpportunity.watched)
    )

    if tab == "solicitations":
        q = q.filter(Opportunity.opportunity_type.in_(solicitation_types))
    else:
        q = q.filter(Opportunity.opportunity_type.in_(rfi_types))

    rows = q.order_by(Opportunity.response_deadline.asc()).all()

    today = date.today()
    opportunities = []
    for opp, sc, pc, my_signal, watched in rows:
        opp.days_until_due = (opp.response_deadline - today).days
        opp.pursue_count = int(sc)
        opp.pass_count = int(pc)
        opp.my_signal = my_signal
        opp.watched = bool(watched) if watched is not None else False
        opportunities.append(opp)

    return templates.TemplateResponse("shortlist.html", {
        "request": request,
        "user": user,
        "opportunities": opportunities,
        "current_tab": tab,
        "active_page": "shortlist",
        "sidebar": get_sidebar(db, user)
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
        
    brief_row = db.query(OpportunityBrief).filter(
        OpportunityBrief.opportunity_id == opp_id
    ).first()

    brief = brief_row.brief_json if (brief_row and brief_row.brief_json) else None
    brief_status = brief_row.status if brief_row else None
    brief_error = brief_row.error_message if brief_row else None

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
        "show_votes": True,
        "brief": brief,
        "brief_status": brief_status,
        "brief_error": brief_error,
        "sidebar": get_sidebar(db, user),
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

def get_sidebar(db: Session, user: User, limit_each: int = 8):
    MyVote = aliased(Vote)
    OrgVote = aliased(Vote)

    shortlist_count = func.coalesce(func.sum(case((OrgVote.vote == "UP", 1), else_=0)), 0)
    pass_count = func.coalesce(func.sum(case((OrgVote.vote.in_(["DOWN", "PASS"]), 1), else_=0)), 0)

    # My Shortlisted = my vote == UP
    my_shortlisted_rows = (
        db.query(Opportunity, shortlist_count.label("shortlist_count"), pass_count.label("pass_count"))
        .join(
            MyVote,
            and_(
                MyVote.opp_id == Opportunity.id,
                MyVote.org_id == user.organization_id,
                MyVote.user_id == user.id,
                MyVote.vote == "UP",
            ),
        )
        .outerjoin(OrgVote, and_(OrgVote.opp_id == Opportunity.id, OrgVote.org_id == user.organization_id))
        .group_by(Opportunity.id)
        .order_by(Opportunity.response_deadline.asc())
        .limit(limit_each)
        .all()
    )

    # Bookmarks = watched true
    bookmarks = (
        db.query(Opportunity)
        .join(UserOpportunity, and_(UserOpportunity.opportunity_id == Opportunity.id, UserOpportunity.user_id == user.id))
        .filter(UserOpportunity.watched.is_(True))
        .order_by(Opportunity.response_deadline.asc())
        .limit(limit_each)
        .all()
    )

    today = date.today()

    my_shortlisted = []
    for opp, sc, pc in my_shortlisted_rows:
        opp.days_until_due = (opp.response_deadline - today).days
        opp.shortlist_count = int(sc)
        opp.pass_count = int(pc)
        my_shortlisted.append(opp)

    for opp in bookmarks:
        opp.days_until_due = (opp.response_deadline - today).days

    return {"my_shortlisted": my_shortlisted, "bookmarks": bookmarks}


# @router.get("/saved")
# async def saved_page(
#     request: Request,
#     state: str | None = "saved",   # saved | bid | no_bid
#     type: str | None = None,
#     db: Session = Depends(get_db)
# ):
#     user = require_user(request, db)
#     if not user:
#         return RedirectResponse(url="/login", status_code=303)

#     # Map URL state → canonical OpportunityState
#     STATE_MAP = {
#         "saved": "SAVED",
#         "bid": "BID",
#         "no_bid": "NO_BID",
#     }
#     state_value = STATE_MAP.get(state, "SAVED")

#     # Base query: decision truth from OpportunityState
#     query = (
#         db.query(
#             Opportunity,
#             UserOpportunity,
#             func.coalesce(func.sum(case((Vote.vote == "UP", 1), else_=0)), 0).label("up"),
#             func.coalesce(func.sum(case((Vote.vote == "DOWN", 1), else_=0)), 0).label("down"),
#         )
#         .join(
#             OpportunityState,
#             and_(
#                 OpportunityState.opp_id == Opportunity.id,
#                 OpportunityState.org_id == user.organization_id,
#                 OpportunityState.state == state_value,
#             ),
#         )
#         .outerjoin(
#             UserOpportunity,
#             and_(
#                 UserOpportunity.opportunity_id == Opportunity.id,
#                 UserOpportunity.user_id == user.id,
#             ),
#         )
#         .outerjoin(
#             Vote,
#             and_(
#                 Vote.opp_id == Opportunity.id,
#                 Vote.org_id == user.organization_id,
#             ),
#         )
#         .group_by(Opportunity.id, UserOpportunity.id)
#     )


#     # Optional type filter
#     if type == "solicitation":
#         solicitation_types = ["Solicitation", "Combined Synopsis/Solicitation", "Award Notice"]
#         query = query.filter(Opportunity.opportunity_type.in_(solicitation_types))
#     elif type == "rfi":
#         rfi_types = ["RFI", "Sources Sought", "Special Notice", "Pre-Solicitation"]
#         query = query.filter(Opportunity.opportunity_type.in_(rfi_types))

#     rows = query.order_by(Opportunity.response_deadline.asc()).all()


#     # Shape results to match template expectations:
#     # item.opportunity, item.notes, item.internal_deadline, item.created_at
#     saved_items = []
#     for opp, user_opp, up, down in rows:
#         saved_items.append(
#             SavedItemVM(
#                 opportunity=opp,
#                 notes=getattr(user_opp, "notes", None) if user_opp else None,
#                 internal_deadline=getattr(user_opp, "internal_deadline", None) if user_opp else None,
#                 created_at=getattr(user_opp, "created_at", None) if user_opp else None,

#                 up_count=int(up),
#                 down_count=int(down),
#                 pass_count=int(pass_),
#             )
#         )

#     return templates.TemplateResponse("saved.html", {
#         "request": request,
#         "user": user,
#         "saved_items": saved_items,
#         "state_filter": state,   # ← used by tabs + template logic
#         "type_filter": type,
#         "active_page": "saved",
#     })

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
@router.post("/opportunity/{opp_id}/watch")
async def toggle_watch(
    request: Request,
    opp_id: int,
    db: Session = Depends(get_db)
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    uo = db.query(UserOpportunity).filter(
        UserOpportunity.user_id == user.id,
        UserOpportunity.opportunity_id == opp_id
    ).first()

    if not uo:
        uo = UserOpportunity(
            user_id=user.id,
            opportunity_id=opp_id,
            watched=True
        )
        db.add(uo)
    else:
        uo.watched = not bool(uo.watched)

    db.commit()

    referer = request.headers.get("referer", "/")
    return RedirectResponse(url=referer, status_code=303)
    
@router.get("/watchlist")
async def org_watchlist(
    request: Request,
    tab: str = "solicitations",
    db: Session = Depends(get_db)
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    solicitation_types = ["Solicitation", "Combined Synopsis/Solicitation", "Award Notice"]
    rfi_types = ["RFI", "Sources Sought", "Special Notice", "Pre-Solicitation"]

    OS = aliased(OpportunityState)
    MyVote = aliased(Vote)

    pursue_sum = func.coalesce(func.sum(case((Vote.vote == "UP", 1), else_=0)), 0)

    q = db.query(
        Opportunity,
        pursue_sum.label("pursue_count"),
        func.coalesce(func.sum(case((Vote.vote.in_(["DOWN", "PASS"]), 1), else_=0), 0)).label("pass_count"),
        MyVote.vote.label("my_signal"),
        UserOpportunity.watched.label("watched"),
    ).outerjoin(
        OS,
        and_(OS.opp_id == Opportunity.id, OS.org_id == user.organization_id)
    ).outerjoin(
        Vote,
        and_(Vote.opp_id == Opportunity.id, Vote.org_id == user.organization_id)
    ).outerjoin(
        MyVote,
        and_(MyVote.opp_id == Opportunity.id, MyVote.org_id == user.organization_id, MyVote.user_id == user.id)
    ).outerjoin(
        UserOpportunity,
        and_(UserOpportunity.opportunity_id == Opportunity.id, UserOpportunity.user_id == user.id)
    ).filter(
        # OPEN only
        or_(OS.id.is_(None), OS.state.notin_(CLOSED_STATES))
    ).group_by(
        Opportunity.id, MyVote.vote, UserOpportunity.watched
    ).filter(
        UserOpportunity.watched.is_(True)
    )


    if tab == "solicitations":
        q = q.filter(Opportunity.opportunity_type.in_(solicitation_types))
    else:
        q = q.filter(Opportunity.opportunity_type.in_(rfi_types))

    rows = q.order_by(Opportunity.response_deadline.asc()).all()

    today = date.today()
    opportunities = []
    for opp, shortlist_count, pass_count, my_signal, watched in rows:
        opp.days_until_due = (opp.response_deadline - today).days
        opp.pursue_count = int(shortlist_count)
        opp.pass_count = int(pass_count)
        opp.my_signal = my_signal
        opp.watched = bool(watched) if watched is not None else False
        opportunities.append(opp)

    return templates.TemplateResponse("watchlist.html", {
        "request": request,
        "user": user,
        "opportunities": opportunities,
        "current_tab": tab,
        "active_page": "watchlist",
        "sidebar": get_sidebar(db, user),
    })

@router.get("/decisions")
async def decisions(
    request: Request,
    state: str = "bid",  # bid | no_bid
    tab: str = "solicitations",
    db: Session = Depends(get_db)
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    STATE_MAP = {"bid": "BID", "no_bid": "NO_BID"}
    state_value = STATE_MAP.get(state, "BID")

    solicitation_types = ["Solicitation", "Combined Synopsis/Solicitation", "Award Notice"]
    rfi_types = ["RFI", "Sources Sought", "Special Notice", "Pre-Solicitation"]

    MyVote = aliased(Vote)

    q = db.query(
        Opportunity,
        func.coalesce(func.sum(case((Vote.vote == "UP", 1), else_=0)), 0).label("pursue_count"),
        func.coalesce(func.sum(case((Vote.vote == "PASS", 1), else_=0)), 0).label("pass_count"),
        MyVote.vote.label("my_signal"),
        UserOpportunity.watched.label("watched"),
    ).join(
        OpportunityState,
        and_(
            OpportunityState.opp_id == Opportunity.id,
            OpportunityState.org_id == user.organization_id,
            OpportunityState.state == state_value,
        )
    ).outerjoin(
        Vote,
        and_(Vote.opp_id == Opportunity.id, Vote.org_id == user.organization_id)
    ).outerjoin(
        MyVote,
        and_(MyVote.opp_id == Opportunity.id, MyVote.org_id == user.organization_id, MyVote.user_id == user.id)
    ).outerjoin(
        UserOpportunity,
        and_(UserOpportunity.opportunity_id == Opportunity.id, UserOpportunity.user_id == user.id)
    ).group_by(
        Opportunity.id, MyVote.vote, UserOpportunity.watched
    )

    if tab == "solicitations":
        q = q.filter(Opportunity.opportunity_type.in_(solicitation_types))
    else:
        q = q.filter(Opportunity.opportunity_type.in_(rfi_types))

    rows = q.order_by(Opportunity.response_deadline.asc()).all()

    today = date.today()
    opportunities = []
    for opp, shortlist_count, pass_count, my_signal, watched in rows:
        opp.days_until_due = (opp.response_deadline - today).days
        opp.pursue_count = int(shortlist_count)
        opp.pass_count = int(pass_count)
        opp.my_signal = my_signal
        opp.watched = bool(watched) if watched is not None else False
        opportunities.append(opp)

    return templates.TemplateResponse("decisions.html", {
        "request": request,
        "user": user,
        "opportunities": opportunities,
        "state_filter": state,     # "bid" | "no_bid"
        "current_tab": tab,
        "active_page": "decisions",
        "sidebar": get_sidebar(db, user)
    })

# @router.get("/saved_for_later")
# async def saved_for_later(
#      request: Request,
#      tab: str = "solicitations",
#      db: Session = Depends(get_db)
#  ):
#      user = require_user(request, db)
#      if not user:
#          return RedirectResponse(url="/login", status_code=303)

#      solicitation_types = ["Solicitation", "Combined Synopsis/Solicitation", "Award Notice"]
#      rfi_types = ["RFI", "Sources Sought", "Special Notice", "Pre-Solicitation"]

#      q = db.query(Opportunity).join(
#          UserOpportunity,
#          and_(
#              UserOpportunity.opportunity_id == Opportunity.id,
#              UserOpportunity.user_id == user.id,
#              UserOpportunity.watched.is_(True),
#          )
#      )

#      if tab == "solicitations":
#          q = q.filter(Opportunity.opportunity_type.in_(solicitation_types))
#      else:
#          q = q.filter(Opportunity.opportunity_type.in_(rfi_types))

#      opportunities = q.order_by(Opportunity.response_deadline.asc()).all()

#      today = date.today()
#      for opp in opportunities:
#          opp.days_until_due = (opp.response_deadline - today).days
#          opp.watched = True

#      return templates.TemplateResponse("saved_for_later.html", {
#          "request": request,
#          "user": user,
#          "opportunities": opportunities,
#          "current_tab": tab,
#          "active_page": "saved"
#      })
