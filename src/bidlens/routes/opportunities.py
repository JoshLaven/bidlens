from datetime import date, datetime, timedelta
from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from collections import OrderedDict
from ..database import get_db
from ..models import Opportunity, User, UserOpportunity, OpportunityStatus, OrgProfile
from ..auth import get_current_user
from ..services import get_vote_counts, get_user_votes
from sqlalchemy import and_, or_
from dataclasses import dataclass
from typing import Optional
from sqlalchemy import func, case
from ..models import Vote
from ..models import OpportunityBrief


router = APIRouter()
templates = Jinja2Templates(directory="src/bidlens/templates")

SOLICITATION_TYPES = ["Solicitation", "Combined Synopsis/Solicitation", "Award Notice"]
RFI_TYPES = ["RFI", "Sources Sought", "Special Notice", "Pre-Solicitation"]


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

    include_kw = _parse_csv(profile.include_keywords)
    exclude_kw = _parse_csv(profile.exclude_keywords)

    if include_kw:
        conditions = [Opportunity.title.ilike(f"%{kw}%") for kw in include_kw]
        query = query.filter(or_(*conditions))

    if exclude_kw:
        for kw in exclude_kw:
            query = query.filter(~Opportunity.title.ilike(f"%{kw}%"))

    include_ag = _parse_csv(profile.include_agencies)
    exclude_ag = _parse_csv(profile.exclude_agencies)

    if include_ag:
        conditions = [Opportunity.agency.ilike(f"%{ag}%") for ag in include_ag]
        query = query.filter(or_(*conditions))

    if exclude_ag:
        for ag in exclude_ag:
            query = query.filter(~Opportunity.agency.ilike(f"%{ag}%"))

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


def _apply_type_tab(query, tab: str):
    """Filter query by solicitation/RFI type tab."""
    if tab == "solicitations":
        return query.filter(Opportunity.opportunity_type.in_(SOLICITATION_TYPES))
    else:
        return query.filter(Opportunity.opportunity_type.in_(RFI_TYPES))


def _enrich_opps(rows, db, user, watched_col=True):
    """Add computed fields (days, vote counts, user vote) to opportunity rows."""
    today = date.today()
    opportunities = []
    for row in rows:
        if watched_col:
            opp, watched = row
            opp.watched = bool(watched) if watched is not None else False
        else:
            opp = row
            opp.watched = False
        opp.days_until_due = (opp.response_deadline - today).days
        opportunities.append(opp)

    opp_ids = [o.id for o in opportunities]
    counts = get_vote_counts(db, opp_ids)
    user_votes = get_user_votes(db, user.id, opp_ids)

    for opp in opportunities:
        c = counts.get(opp.id, {"pursue": 0, "pass": 0})
        opp.pursue_count = c["pursue"]
        opp.pass_count = c["pass"]
        opp.user_vote = user_votes.get(opp.id)

    return opportunities


def _opp_list_query(db: Session, user, decision_state: str, tab: str):
    """Base query for feed/shortlist/archive list views."""
    q = (
        db.query(Opportunity, UserOpportunity.watched.label("watched"))
        .outerjoin(
            UserOpportunity,
            and_(
                UserOpportunity.opportunity_id == Opportunity.id,
                UserOpportunity.user_id == user.id,
            ),
        )
        .filter(Opportunity.decision_state == decision_state)
    )
    q = _apply_type_tab(q, tab)
    return q


# ── Feed (INBOX) ──────────────────────────────────────────────

@router.get("/")
async def feed(
    request: Request,
    tab: str = "solicitations",
    sort: str = "newest",
    date_filter: str = "",
    date_from: str = "",
    date_to: str = "",
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    q = _opp_list_query(db, user, "INBOX", tab)
    q = apply_org_filters(q, db, user)

    # Date filtering on upserted_at
    today = date.today()
    if date_filter == "today":
        q = q.filter(func.date(Opportunity.upserted_at) >= today)
    elif date_filter == "7d":
        q = q.filter(func.date(Opportunity.upserted_at) >= today - timedelta(days=7))
    elif date_filter == "30d":
        q = q.filter(func.date(Opportunity.upserted_at) >= today - timedelta(days=30))
    elif date_filter == "custom" and date_from:
        try:
            d_from = date.fromisoformat(date_from)
            q = q.filter(func.date(Opportunity.upserted_at) >= d_from)
        except ValueError:
            pass
        if date_to:
            try:
                d_to = date.fromisoformat(date_to)
                q = q.filter(func.date(Opportunity.upserted_at) <= d_to)
            except ValueError:
                pass

    # Sort
    if sort == "deadline":
        q = q.order_by(Opportunity.response_deadline.asc())
    elif sort == "posted":
        q = q.order_by(Opportunity.posted_date.desc())
    else:  # "newest" — default
        q = q.order_by(Opportunity.upserted_at.desc())

    rows = q.limit(50).all()

    return templates.TemplateResponse("feed.html", {
        "request": request,
        "user": user,
        "opportunities": _enrich_opps(rows, db, user),
        "current_tab": tab,
        "active_page": "feed",
        "sidebar": get_sidebar(db, user),
        "sort": sort,
        "date_filter": date_filter,
        "date_from": date_from,
        "date_to": date_to,
        "now": datetime.utcnow(),
    })


# ── Shortlist (SHORTLISTED) ──────────────────────────────────

@router.get("/shortlist")
async def shortlist(
    request: Request,
    tab: str = "solicitations",
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    q = _opp_list_query(db, user, "SHORTLISTED", tab)
    rows = q.order_by(Opportunity.response_deadline.asc()).all()

    opps = _enrich_opps(rows, db, user)
    opps.sort(key=lambda o: o.pursue_count, reverse=True)

    return templates.TemplateResponse("shortlist.html", {
        "request": request,
        "user": user,
        "opportunities": opps,
        "current_tab": tab,
        "active_page": "shortlist",
        "sidebar": get_sidebar(db, user),
    })


# ── My Shortlist (SHORTLISTED + following) ───────────────────

@router.get("/my-shortlist")
async def my_shortlist(
    request: Request,
    tab: str = "solicitations",
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    q = (
        db.query(Opportunity, UserOpportunity.watched.label("watched"))
        .join(
            UserOpportunity,
            and_(
                UserOpportunity.opportunity_id == Opportunity.id,
                UserOpportunity.user_id == user.id,
            ),
        )
        .filter(
            Opportunity.decision_state == "SHORTLISTED",
            UserOpportunity.watched.is_(True),
        )
    )
    q = _apply_type_tab(q, tab)
    rows = q.order_by(Opportunity.response_deadline.asc().nullslast()).all()

    opps = _enrich_opps(rows, db, user)

    return templates.TemplateResponse("my_shortlist.html", {
        "request": request,
        "user": user,
        "opportunities": opps,
        "current_tab": tab,
        "active_page": "my_shortlist",
        "sidebar": get_sidebar(db, user),
    })


# ── Archive (ARCHIVED) ───────────────────────────────────────

@router.get("/archive")
async def archive(
    request: Request,
    tab: str = "solicitations",
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    q = _opp_list_query(db, user, "ARCHIVED", tab)
    rows = q.order_by(Opportunity.response_deadline.asc()).all()

    return templates.TemplateResponse("archive.html", {
        "request": request,
        "user": user,
        "opportunities": _enrich_opps(rows, db, user),
        "current_tab": tab,
        "active_page": "archive",
        "sidebar": get_sidebar(db, user),
    })


# ── Detail ────────────────────────────────────────────────────

@router.get("/opportunity/{opp_id}")
async def opportunity_detail(
    request: Request,
    opp_id: int,
    db: Session = Depends(get_db),
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

    user_opp = db.query(UserOpportunity).filter(
        UserOpportunity.user_id == user.id,
        UserOpportunity.opportunity_id == opp_id,
    ).first()

    today = date.today()
    days_until_due = (opportunity.response_deadline - today).days

    counts = get_vote_counts(db, [opp_id])
    c = counts.get(opp_id, {"pursue": 0, "pass": 0})
    user_votes = get_user_votes(db, user.id, [opp_id])

    return templates.TemplateResponse("detail.html", {
        "request": request,
        "user": user,
        "opportunity": opportunity,
        "user_opp": user_opp,
        "decision_state": opportunity.decision_state,
        "days_until_due": days_until_due,
        "pursue_count": c["pursue"],
        "pass_count": c["pass"],
        "user_vote": user_votes.get(opp_id),
        "active_page": None,
        "brief": brief,
        "brief_status": brief_status,
        "brief_error": brief_error,
        "sidebar": get_sidebar(db, user),
    })


# ── User-level actions (notes, bookmark, etc.) ───────────────

@router.post("/opportunity/{opp_id}/update")
async def update_opportunity(
    request: Request,
    opp_id: int,
    internal_deadline: str = Form(None),
    notes: str = Form(None),
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    user_opp = db.query(UserOpportunity).filter(
        UserOpportunity.user_id == user.id,
        UserOpportunity.opportunity_id == opp_id,
    ).first()

    if not user_opp:
        user_opp = UserOpportunity(user_id=user.id, opportunity_id=opp_id)
        db.add(user_opp)
        db.flush()

    if internal_deadline:
        user_opp.internal_deadline = datetime.strptime(internal_deadline, "%Y-%m-%d").date()
    else:
        user_opp.internal_deadline = None

    user_opp.notes = notes if notes else None
    db.commit()

    return RedirectResponse(url=f"/opportunity/{opp_id}", status_code=303)


@router.post("/opportunity/{opp_id}/watch")
async def toggle_watch(
    request: Request,
    opp_id: int,
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    uo = db.query(UserOpportunity).filter(
        UserOpportunity.user_id == user.id,
        UserOpportunity.opportunity_id == opp_id,
    ).first()

    if not uo:
        uo = UserOpportunity(user_id=user.id, opportunity_id=opp_id, watched=True)
        db.add(uo)
    else:
        uo.watched = not bool(uo.watched)

    db.commit()

    referer = request.headers.get("referer", "/")
    return RedirectResponse(url=referer, status_code=303)


# ── Calendar ──────────────────────────────────────────────────

@router.get("/calendar")
async def calendar_page(
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    saved_items = db.query(UserOpportunity).filter(
        UserOpportunity.user_id == user.id,
        UserOpportunity.status.in_([OpportunityStatus.SAVED, OpportunityStatus.IN_PROGRESS]),
    ).all()

    saved_items = [item for item in saved_items if item.opportunity.opportunity_type in SOLICITATION_TYPES]

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
        "active_page": "calendar",
    })


# ── Sidebar ───────────────────────────────────────────────────

def get_sidebar(db: Session, user: User):
    """Sidebar: My Shortlisted – Due Soon + Following."""
    today = date.today()

    # My Shortlisted – Due Soon: SHORTLISTED + user is following, sorted by deadline
    my_shortlisted = (
        db.query(Opportunity)
        .join(
            UserOpportunity,
            and_(
                UserOpportunity.opportunity_id == Opportunity.id,
                UserOpportunity.user_id == user.id,
            ),
        )
        .filter(
            Opportunity.decision_state == "SHORTLISTED",
            UserOpportunity.watched.is_(True),
        )
        .order_by(Opportunity.response_deadline.asc())
        .limit(5)
        .all()
    )

    # Following: all opps user follows (any state)
    following = (
        db.query(Opportunity)
        .join(
            UserOpportunity,
            and_(
                UserOpportunity.opportunity_id == Opportunity.id,
                UserOpportunity.user_id == user.id,
            ),
        )
        .filter(UserOpportunity.watched.is_(True))
        .order_by(Opportunity.response_deadline.asc())
        .limit(8)
        .all()
    )

    for opp in my_shortlisted:
        opp.days_until_due = (opp.response_deadline - today).days

    for opp in following:
        opp.days_until_due = (opp.response_deadline - today).days

    return {"my_shortlisted": my_shortlisted, "following": following}
