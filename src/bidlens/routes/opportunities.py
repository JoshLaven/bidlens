from datetime import date, datetime, timedelta
import html
import re
import csv
import io
from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload
from collections import OrderedDict
from ..database import get_db
from ..models import Opportunity, User, UserOpportunity, OpportunityStatus, OrgProfile, OpportunityNote
from ..auth import get_current_user
from ..services import get_vote_counts, get_user_votes, get_last_activity, get_vote_user_maps
from sqlalchemy import and_, or_
from dataclasses import dataclass
from typing import Optional
from sqlalchemy import func, case
from ..models import Vote
from ..models import OpportunityBrief
from ..tenancy import current_org_id
router = APIRouter()
templates = Jinja2Templates(directory="src/bidlens/templates")

SOLICITATION_TYPES = ["Solicitation", "Combined Synopsis/Solicitation", "Award Notice"]
RFI_TYPES = ["RFI", "Sources Sought", "Special Notice", "Pre-Solicitation"]
BRIEF_SECTION_DEFS = [
    ("executive_summary", "Executive Summary"),
    ("key_dates", "Key Dates"),
    ("buyer_agency", "Buyer / Agency"),
    ("scope_of_work", "Scope of Work"),
    ("eligibility_set_aside", "Eligibility / Set-Aside"),
    ("submission_requirements", "Submission Requirements"),
    ("evaluation_criteria", "Evaluation Criteria"),
    ("fit_signals", "Fit Signals"),
    ("risk_flags", "Risk Flags"),
    ("open_questions", "Open Questions"),
    ("recommended_action", "Recommended Action"),
]


def _normalize_brief_status(value: str | None) -> str:
    if value == "pending":
        return "generating"
    if value == "ok":
        return "completed"
    return value or "not_started"


def _is_url_like(value):
    if value is None:
        return False
    s = str(value).strip().lower()
    return s.startswith("http://") or s.startswith("https://") or s.startswith("www.")


def _parse_csv(text: str | None) -> list[str]:
    """Split comma-separated OrgProfile field into a cleaned list."""
    if not text:
        return []
    return [s.strip() for s in text.split(",") if s.strip()]


def _normalize_brief_section_items(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, dict):
        return [f"{k}: {v}".strip() for k, v in value.items() if str(v).strip()]
    return [str(value).strip()]


def _build_brief_sections(brief: dict | None) -> list[dict]:
    if not brief:
        return []

    legacy = {
        "executive_summary": brief.get("summary_bullets"),
        "scope_of_work": brief.get("deliverables"),
        "eligibility_set_aside": brief.get("eligibility"),
        "submission_requirements": brief.get("key_requirements"),
        "risk_flags": brief.get("red_flags"),
        "recommended_action": brief.get("recommended_next_steps"),
    }

    sections: list[dict] = []
    for key, label in BRIEF_SECTION_DEFS:
        raw_value = brief.get(key)
        if raw_value is None and key in legacy:
            raw_value = legacy[key]
        items = _normalize_brief_section_items(raw_value)
        if not items:
            items = ["Not found in available materials"]
        sections.append({"key": key, "label": label, "items": items})
    return sections


def apply_org_filters(query, db: Session, user):
    """Apply OrgProfile keyword/agency/deadline filters to an Opportunity query."""
    profile = db.query(OrgProfile).filter(OrgProfile.org_id == _user_org_id(user)).first()
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
    setattr(user, "current_organization_id", current_org_id(request, db, user))
    return user


def _user_org_id(user) -> int:
    return getattr(user, "current_organization_id", None) or user.organization_id


def _apply_type_tab(query, tab: str):
    """Filter query by solicitation/RFI type tab."""
    if tab == "solicitations":
        return query.filter(Opportunity.opportunity_type.in_(SOLICITATION_TYPES))
    else:
        return query.filter(Opportunity.opportunity_type.in_(RFI_TYPES))


def _agency_parts_for_export(raw_agency: str | None) -> tuple[str | None, str | None]:
    if not raw_agency:
        return None, None

    cleaned_parts = [
        part.replace("_", " ").strip()
        for part in str(raw_agency).split(".")
        if part and part.strip()
    ]
    if not cleaned_parts:
        return None, None

    department = cleaned_parts[0].title()
    sub_agency = cleaned_parts[-1].title() if len(cleaned_parts) > 1 else None
    if sub_agency == department:
        sub_agency = None
    return department, sub_agency


def _current_org_status(opp: Opportunity) -> str:
    stage = (opp.review_stage or "").strip()
    if opp.decision_state == "SHORTLISTED" and stage:
        return f"{opp.decision_state} / {stage}"
    if opp.decision_state == "ARCHIVED" and opp.archived_reason:
        return f"{opp.decision_state} / {opp.archived_reason}"
    return opp.decision_state


def _base_export_filename(view: str, tab: str) -> str:
    safe_view = (view or "feed").replace("-", "_")
    safe_tab = (tab or "solicitations").replace("-", "_")
    return f"bidlens_{safe_view}_{safe_tab}_export.csv"


def _format_date(value) -> str:
    if not value:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)


def _export_view_query(
    db: Session,
    user,
    *,
    view: str,
    tab: str,
    sort: str = "newest",
    date_filter: str = "",
    date_from: str = "",
    date_to: str = "",
    show_passed: str = "",
    show_past_due: str = "",
):
    if view == "shortlist":
        q = _opp_list_query(db, user, "SHORTLISTED", tab)
        q = _apply_past_due_filter(q, show_past_due=show_past_due)
    elif view == "archive":
        q = _opp_list_query(db, user, "ARCHIVED", tab)
    elif view == "my_shortlist":
        q = _my_shortlist_query(db, user, tab)
    else:
        q = _opp_list_query(db, user, "INBOX", tab)
        q = apply_org_filters(q, db, user)
        q = _apply_past_due_filter(q, show_past_due=show_past_due)
        if show_passed != "1":
            passed_opp_ids = (
                db.query(Vote.opp_id)
                .filter(Vote.org_id == _user_org_id(user), Vote.user_id == user.id, Vote.vote == "PASS")
                .subquery()
            )
            q = q.filter(~Opportunity.id.in_(passed_opp_ids))

        date_field = Opportunity.upserted_at
        if sort == "deadline":
            date_field = Opportunity.response_deadline
        elif sort == "posted":
            date_field = Opportunity.posted_date

        today = date.today()
        if date_filter == "today":
            q = q.filter(func.date(date_field) >= today)
        elif date_filter == "7d":
            q = q.filter(func.date(date_field) >= today - timedelta(days=7))
        elif date_filter == "30d":
            q = q.filter(func.date(date_field) >= today - timedelta(days=30))
        elif date_filter == "custom" and date_from:
            try:
                d_from = date.fromisoformat(date_from)
                q = q.filter(func.date(date_field) >= d_from)
            except ValueError:
                pass
            if date_to:
                try:
                    d_to = date.fromisoformat(date_to)
                    q = q.filter(func.date(date_field) <= d_to)
                except ValueError:
                    pass

    return q


def _vote_export_maps(db: Session, org_id: int, opp_ids: list[int]) -> tuple[dict[int, dict[str, int]], dict[int, list[str]], dict[int, list[str]]]:
    if not opp_ids:
        return {}, {}, {}

    counts = get_vote_counts(db, opp_ids)
    shortlist_users, pass_users = get_vote_user_maps(db, org_id=org_id, opp_ids=opp_ids)
    return counts, shortlist_users, pass_users


def _sort_export_opportunities(db: Session, opportunities: list[Opportunity], *, view: str, sort: str) -> list[Opportunity]:
    if not opportunities:
        return opportunities

    if view == "shortlist":
        if sort == "activity":
            opp_ids = [opp.id for opp in opportunities]
            activity_map = get_last_activity(db, opp_ids)
            for opp in opportunities:
                opp.last_activity = activity_map.get(opp.id)
            opportunities.sort(key=lambda opp: opp.last_activity or datetime.min, reverse=True)
        elif sort == "pursue":
            opportunities.sort(key=lambda opp: (opp.pursue_count, opp.response_deadline or date.max), reverse=True)
        else:
            opportunities.sort(key=lambda opp: (opp.response_deadline or date.max, opp.id))
    elif view == "feed":
        if sort == "deadline":
            opportunities.sort(key=lambda opp: (opp.response_deadline or date.max, opp.id))
        elif sort == "posted":
            opportunities.sort(key=lambda opp: (opp.posted_date or date.min, opp.id), reverse=True)
        else:
            opportunities.sort(key=lambda opp: (opp.upserted_at or datetime.min, opp.id), reverse=True)
    else:
        opportunities.sort(key=lambda opp: (opp.response_deadline or date.max, opp.id))

    return opportunities


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
    pursue_users_map, pass_users_map = get_vote_user_maps(db, org_id=_user_org_id(user), opp_ids=opp_ids)

    for opp in opportunities:
        c = counts.get(opp.id, {"pursue": 0, "pass": 0})
        opp.pursue_count = c["pursue"]
        opp.pass_count = c["pass"]
        opp.user_vote = user_votes.get(opp.id)
        opp.pursue_users = pursue_users_map.get(opp.id, [])
        opp.pass_users = pass_users_map.get(opp.id, [])
        opp.preview_description = _clean_preview_text(opp.description_text or opp.description or "")
        opp.preview_has_sam_fallback = bool((not opp.preview_description) and getattr(opp, "sam_url", None))

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
        .filter(Opportunity.organization_id == _user_org_id(user))
    )
    q = _apply_type_tab(q, tab)
    return q


def _my_shortlist_query(db: Session, user, tab: str):
    """User's personal shortlist: org-shortlisted + this user voted PURSUE."""
    q = (
        db.query(Opportunity, UserOpportunity.watched.label("watched"))
        .join(
            Vote,
            and_(
                Vote.opp_id == Opportunity.id,
                Vote.org_id == _user_org_id(user),
                Vote.user_id == user.id,
                Vote.vote == "PURSUE",
            ),
        )
        .outerjoin(
            UserOpportunity,
            and_(
                UserOpportunity.opportunity_id == Opportunity.id,
                UserOpportunity.user_id == user.id,
            ),
        )
        .filter(Opportunity.decision_state == "SHORTLISTED")
        .filter(Opportunity.organization_id == _user_org_id(user))
    )
    q = _apply_type_tab(q, tab)
    return q


def _best_description_text(opportunity: Opportunity) -> str:
    description_text = (opportunity.description_text or "").strip()
    if description_text:
        return description_text

    description = (opportunity.description or "").strip()
    if description and not _is_url_like(description):
        return description

    return ""


def _clean_preview_text(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(value))
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _apply_past_due_filter(query, *, show_past_due: str = ""):
    if show_past_due == "1":
        return query
    today = date.today()
    return query.filter(
        or_(
            Opportunity.response_deadline.is_(None),
            Opportunity.response_deadline >= today,
        )
    )


# ── Feed (INBOX) ──────────────────────────────────────────────

@router.get("/")
async def feed(
    request: Request,
    tab: str = "solicitations",
    sort: str = "newest",
    date_filter: str = "",
    date_from: str = "",
    date_to: str = "",
    show_passed: str = "",
    show_past_due: str = "",
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    q = _opp_list_query(db, user, "INBOX", tab)
    q = apply_org_filters(q, db, user)
    q = _apply_past_due_filter(q, show_past_due=show_past_due)

    # By default, hide items the current user voted PASS on.
    # ?show_passed=1 reveals them.
    if show_passed != "1":
        passed_opp_ids = (
            db.query(Vote.opp_id)
            .filter(Vote.org_id == _user_org_id(user), Vote.user_id == user.id, Vote.vote == "PASS")
            .subquery()
        )
        q = q.filter(~Opportunity.id.in_(passed_opp_ids))

    date_field = Opportunity.upserted_at
    if sort == "deadline":
        date_field = Opportunity.response_deadline
    elif sort == "posted":
        date_field = Opportunity.posted_date

    # Date filtering on the currently selected date basis
    today = date.today()
    if date_filter == "today":
        q = q.filter(func.date(date_field) >= today)
    elif date_filter == "7d":
        q = q.filter(func.date(date_field) >= today - timedelta(days=7))
    elif date_filter == "30d":
        q = q.filter(func.date(date_field) >= today - timedelta(days=30))
    elif date_filter == "custom" and date_from:
        try:
            d_from = date.fromisoformat(date_from)
            q = q.filter(func.date(date_field) >= d_from)
        except ValueError:
            pass
        if date_to:
            try:
                d_to = date.fromisoformat(date_to)
                q = q.filter(func.date(date_field) <= d_to)
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
        "date_basis": sort,
        "show_passed": show_passed,
        "show_past_due": show_past_due,
        "now": datetime.utcnow(),
    })


REVIEW_STAGES = ["Team Review", "Director Review", "Approved"]


# ── Shortlist (SHORTLISTED) ──────────────────────────────────

@router.get("/shortlist")
async def shortlist(
    request: Request,
    tab: str = "solicitations",
    sort: str = "pursue",
    show_past_due: str = "",
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    q = _opp_list_query(db, user, "SHORTLISTED", tab)
    q = _apply_past_due_filter(q, show_past_due=show_past_due)
    rows = q.order_by(Opportunity.response_deadline.asc()).all()

    opps = _enrich_opps(rows, db, user)

    # Attach last_activity timestamp per opp
    opp_ids = [o.id for o in opps]
    activity_map = get_last_activity(db, opp_ids)
    for opp in opps:
        opp.last_activity = activity_map.get(opp.id)

    # Sort
    if sort == "deadline":
        opps.sort(key=lambda o: o.response_deadline)
    elif sort == "activity":
        opps.sort(key=lambda o: o.last_activity or datetime.min, reverse=True)
    else:  # "pursue" — default
        opps.sort(key=lambda o: o.pursue_count, reverse=True)

    return templates.TemplateResponse("shortlist.html", {
        "request": request,
        "user": user,
        "opportunities": opps,
        "current_tab": tab,
        "active_page": "shortlist",
        "sidebar": get_sidebar(db, user),
        "sort": sort,
        "show_past_due": show_past_due,
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

    q = _my_shortlist_query(db, user, tab)
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


@router.get("/opportunities/export.csv")
async def export_opportunities_csv(
    request: Request,
    view: str = "feed",
    tab: str = "solicitations",
    sort: str = "newest",
    date_filter: str = "",
    date_from: str = "",
    date_to: str = "",
    show_passed: str = "",
    show_past_due: str = "",
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    q = _export_view_query(
        db,
        user,
        view=view,
        tab=tab,
        sort=sort,
        date_filter=date_filter,
        date_from=date_from,
        date_to=date_to,
        show_passed=show_passed,
        show_past_due=show_past_due,
    )

    rows = q.all()
    opportunities = _enrich_opps(rows, db, user)
    opportunities = _sort_export_opportunities(db, opportunities, view=view, sort=sort)
    opp_ids = [opp.id for opp in opportunities]

    vote_counts, shortlist_users_map, pass_users_map = _vote_export_maps(db, _user_org_id(user), opp_ids)
    user_votes = get_user_votes(db, user.id, opp_ids)

    user_opp_rows = (
        db.query(UserOpportunity)
        .filter(
            UserOpportunity.user_id == user.id,
            UserOpportunity.opportunity_id.in_(opp_ids),
        )
        .all()
        if opp_ids else []
    )
    user_opp_map = {row.opportunity_id: row for row in user_opp_rows}

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "BidLens ID",
        "DB ID",
        "Title",
        "Agency",
        "Department",
        "Sub-agency",
        "Opportunity Type",
        "Posted Date",
        "Response Deadline",
        "Days Until Due",
        "NAICS",
        "Set-Aside",
        "SAM Notice ID",
        "SAM URL",
        "Current Org-Level Status",
        "Shortlist Count",
        "Pass Count",
        "Users Who Shortlisted",
        "Users Who Passed",
        "Current User Vote",
        "Watched",
        "My Shortlist",
        "Internal Deadline",
        "Notes",
    ])

    today = date.today()
    for opp in opportunities:
        department, sub_agency = _agency_parts_for_export(opp.agency)
        counts = vote_counts.get(opp.id, {"pursue": getattr(opp, "pursue_count", 0), "pass": getattr(opp, "pass_count", 0)})
        shortlist_users = "; ".join(shortlist_users_map.get(opp.id, []))
        pass_users = "; ".join(pass_users_map.get(opp.id, []))
        user_opp = user_opp_map.get(opp.id)
        current_vote = user_votes.get(opp.id) or getattr(opp, "user_vote", None) or ""
        my_shortlist = "Yes" if current_vote == "PURSUE" else "No"
        watched = "Yes" if bool(getattr(opp, "watched", False) or (user_opp and user_opp.watched)) else "No"
        days_until_due = ""
        if opp.response_deadline:
            days_until_due = (opp.response_deadline - today).days

        writer.writerow([
            str(opp.bidlens_id or ""),
            opp.id,
            opp.title or "",
            opp.agency or "",
            department or "",
            sub_agency or "",
            opp.opportunity_type or "",
            _format_date(opp.posted_date),
            _format_date(opp.response_deadline),
            days_until_due,
            opp.naics or "",
            opp.set_aside or "",
            opp.sam_notice_id or "",
            opp.sam_url or "",
            _current_org_status(opp),
            counts.get("pursue", 0),
            counts.get("pass", 0),
            shortlist_users,
            pass_users,
            current_vote,
            watched,
            my_shortlist,
            _format_date(user_opp.internal_deadline) if user_opp else "",
            user_opp.notes if user_opp and user_opp.notes else "",
        ])

    filename = _base_export_filename(view, tab)
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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

    opportunity = db.query(Opportunity).filter(
        Opportunity.id == opp_id,
        Opportunity.organization_id == _user_org_id(user),
    ).first()
    if not opportunity:
        return RedirectResponse(url="/", status_code=303)

    resolved_description = _best_description_text(opportunity) or None

    brief_row = db.query(OpportunityBrief).filter(
        OpportunityBrief.opportunity_id == opp_id,
        OpportunityBrief.organization_id == _user_org_id(user),
    ).first()

    brief = brief_row.brief_json if (brief_row and brief_row.brief_json) else None
    brief_sections = _build_brief_sections(brief)
    brief_status = _normalize_brief_status(brief_row.status if brief_row else None)
    brief_error = brief_row.error_message if brief_row else None
    brief_source_basis = brief_row.source_basis if brief_row else None
    brief_source_files = brief_row.filenames_processed if (brief_row and brief_row.filenames_processed) else []
    brief_source_summary = brief_row.source_summary if (brief_row and brief_row.source_summary) else None
    brief_generated_at = brief_row.generated_at if brief_row else None
    brief_provider = brief_row.provider if brief_row else None
    brief_model = brief_row.model if brief_row else None

    user_opp = db.query(UserOpportunity).filter(
        UserOpportunity.user_id == user.id,
        UserOpportunity.opportunity_id == opp_id,
    ).first()

    today = date.today()
    days_until_due = (opportunity.response_deadline - today).days

    counts = get_vote_counts(db, [opp_id])
    c = counts.get(opp_id, {"pursue": 0, "pass": 0})
    user_votes = get_user_votes(db, user.id, [opp_id])
    notes = (
        db.query(OpportunityNote)
        .options(joinedload(OpportunityNote.user))
        .filter(
            OpportunityNote.opportunity_id == opp_id,
            OpportunityNote.org_id == _user_org_id(user),
        )
        .order_by(OpportunityNote.created_at.desc())
        .all()
    )

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
        "brief_sections": brief_sections,
        "brief_status": brief_status,
        "brief_error": brief_error,
        "brief_generated_at": brief_generated_at,
        "brief_provider": brief_provider,
        "brief_model": brief_model,
        "brief_source_basis": brief_source_basis,
        "brief_source_files": brief_source_files,
        "brief_source_summary": brief_source_summary,
        "resolved_description": resolved_description,
        "opportunity_notes": notes,
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
        UserOpportunity.organization_id == _user_org_id(user),
        UserOpportunity.user_id == user.id,
        UserOpportunity.opportunity_id == opp_id,
    ).first()

    if not user_opp:
        user_opp = UserOpportunity(organization_id=_user_org_id(user), user_id=user.id, opportunity_id=opp_id)
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
        UserOpportunity.organization_id == _user_org_id(user),
        UserOpportunity.user_id == user.id,
        UserOpportunity.opportunity_id == opp_id,
    ).first()

    if not uo:
        uo = UserOpportunity(organization_id=_user_org_id(user), user_id=user.id, opportunity_id=opp_id, watched=True)
        db.add(uo)
    else:
        uo.watched = not bool(uo.watched)

    db.commit()

    referer = request.headers.get("referer", "/")
    return RedirectResponse(url=referer, status_code=303)


@router.post("/opportunities/{opp_id}/notes")
async def add_opportunity_note(
    request: Request,
    opp_id: int,
    body: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    opportunity = db.query(Opportunity).filter(
        Opportunity.id == opp_id,
        Opportunity.organization_id == _user_org_id(user),
    ).first()
    if not opportunity:
        return RedirectResponse(url="/", status_code=303)

    note_body = (body or "").strip()
    if not note_body:
        return RedirectResponse(url=f"/opportunity/{opp_id}", status_code=303)

    note = OpportunityNote(
        org_id=_user_org_id(user),
        opportunity_id=opp_id,
        user_id=user.id,
        body=note_body,
    )
    db.add(note)
    db.commit()

    return RedirectResponse(url=f"/opportunity/{opp_id}", status_code=303)


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
        UserOpportunity.organization_id == _user_org_id(user),
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

    # My Shortlisted – Due Soon: SHORTLISTED + this user voted PURSUE
    my_shortlisted = (
        db.query(Opportunity)
        .join(
            Vote,
            and_(
                Vote.opp_id == Opportunity.id,
                Vote.org_id == _user_org_id(user),
                Vote.user_id == user.id,
                Vote.vote == "PURSUE",
            ),
        )
        .filter(Opportunity.decision_state == "SHORTLISTED")
        .filter(Opportunity.organization_id == _user_org_id(user))
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
                UserOpportunity.organization_id == _user_org_id(user),
            ),
        )
        .filter(UserOpportunity.watched.is_(True))
        .filter(Opportunity.organization_id == _user_org_id(user))
        .order_by(Opportunity.response_deadline.asc())
        .limit(8)
        .all()
    )

    for opp in my_shortlisted:
        opp.days_until_due = (opp.response_deadline - today).days

    for opp in following:
        opp.days_until_due = (opp.response_deadline - today).days

    return {"my_shortlisted": my_shortlisted, "following": following}
