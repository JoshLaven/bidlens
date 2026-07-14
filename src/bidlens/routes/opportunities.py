from datetime import date, datetime, timedelta
import html
import logging
import re
import csv
import io
import requests
from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload
from collections import OrderedDict
from ..database import get_db
from ..models import (
    Opportunity,
    User,
    UserOpportunity,
    OpportunityStatus,
    OpportunityNote,
    OpportunityHistoryEvent,
    OrganizationMembership,
)
from ..auth import attach_request_user_context, get_current_user
from ..services import get_vote_counts, get_user_votes, get_last_activity, get_vote_user_maps
from ..services.opportunity_history import unread_history_count
from ..services.opportunity_stages import (
    DISPLAY_STAGES,
    RFI_TYPE_INDICATORS,
    normalize_display_stage,
)
from ..services.feed_queries import (
    apply_org_feed_filters,
    build_feed_query,
    exclude_past_due_opportunities,
    feed_awaiting_review_query,
)
from ..services.pursuit_lanes import user_my_lanes
from ..services.platform import pre_live_admin_setup_url
from sqlalchemy import and_, or_, select
from dataclasses import dataclass
from typing import Optional
from sqlalchemy import func, case
from ..models import OpportunityPursuitLaneMatch, PursuitLane, Vote
from ..models import OpportunityBrief
from ..grants_gov_client import GrantsGovApiError
from ..ingest_grants_gov import enrich_grants_gov_opportunity_detail
router = APIRouter()
templates = Jinja2Templates(directory="src/bidlens/templates")
logger = logging.getLogger(__name__)

QUALIFICATION_UNREVIEWED = "unreviewed"
QUALIFICATION_QUALIFIED = "qualified"
QUALIFICATION_REJECTED = "rejected"
QUALIFICATION_STATUSES = {
    QUALIFICATION_UNREVIEWED,
    QUALIFICATION_QUALIFIED,
    QUALIFICATION_REJECTED,
}
DATE_TYPE_IMPORTED = "imported"
DATE_TYPE_DUE = "due"
DATE_TYPE_POSTED = "posted"
DATE_TYPES = {DATE_TYPE_IMPORTED, DATE_TYPE_DUE, DATE_TYPE_POSTED}
FEED_PAGE_SIZE = 50
TRIAGE_PAGE_SIZE = 100
TRIAGE_SOURCE_FILTERS = ("sam", "grants", "govwin")
TRIAGE_SOURCE_OPTIONS = (
    {"value": "sam", "label": "SAM.gov"},
    {"value": "grants", "label": "Grants.gov"},
    {"value": "govwin", "label": "GovWin"},
)
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
    return apply_org_feed_filters(query, db, organization_id=_user_org_id(user))


def require_user(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None
    attach_request_user_context(request, db, user)
    return user


def _pre_live_product_redirect(db: Session, user) -> RedirectResponse | None:
    setup_url = pre_live_admin_setup_url(
        db,
        user,
        organization_id=_user_org_id(user),
    )
    if setup_url:
        return RedirectResponse(url=setup_url, status_code=303)
    return None


def _user_org_id(user) -> int:
    return getattr(user, "current_organization_id", None) or user.organization_id


def _current_user_role(db: Session, user) -> str:
    org_id = _user_org_id(user)
    membership = (
        db.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == org_id,
            OrganizationMembership.user_id == user.id,
        )
        .first()
    )
    return membership.role if membership else "member"


def _is_admin(user) -> bool:
    return getattr(user, "current_role", "member") == "admin"


def _history_source_label(source: str | None) -> str:
    return {
        "sam": "SAM.gov",
        "sam.gov": "SAM.gov",
        "grants_gov": "Grants.gov",
        "govwin_export": "GovWin",
        "govwin_api": "GovWin",
        "salesforce": "Salesforce",
    }.get(str(source or "").strip().lower(), str(source or "source"))


def _history_field_label(value: object) -> str:
    text = str(value or "").strip().replace("_", " ")
    if not text:
        return ""
    field_labels = {
        "cfdas": "Assistance Listings",
        "alns": "Assistance Listings",
        "synopsisDesc": "Synopsis Description",
        "forecastDesc": "Forecast Description",
        "applicantEligibilityDesc": "Applicant Eligibility",
        "agencyContactDesc": "Agency Contact",
        "fundingDescLinkUrl": "Funding Description Link",
        "fundingDescLinkDesc": "Funding Description Link Label",
    }
    if text in field_labels:
        return field_labels[text]
    words: list[str] = []
    current = ""
    for character in text:
        if character.isupper() and current and not current[-1].isupper():
            words.append(current)
            current = character
        else:
            current += character
    if current:
        words.append(current)
    return " ".join(words).strip().title()


def _grants_updated_date_label(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    for timezone_suffix in (" EDT", " EST", " UTC", " GMT"):
        if text.endswith(timezone_suffix):
            text = text[: -len(timezone_suffix)]
            break
    for date_format in (
        "%b %d, %Y %I:%M:%S %p",
        "%Y-%m-%d-%H-%M-%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            parsed = datetime.strptime(text, date_format)
            return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}"
        except ValueError:
            continue
    return str(value)


def _prepare_history_events(events: list[OpportunityHistoryEvent]) -> list[OpportunityHistoryEvent]:
    today = date.today()
    titles = {
        "opportunity_imported": "Opportunity imported from {source}",
        "source_updated": "Opportunity updated from {source}",
        "salesforce_synchronized": "Synchronized to Salesforce",
        "grants_synopsis_version": "Grants.gov version update",
        "grants_forecast_version": "Grants.gov version update",
    }
    for event in events:
        event_date = event.occurred_at.date()
        if event_date == today:
            event.day_label = "Today"
        elif event_date == today - timedelta(days=1):
            event.day_label = "Yesterday"
        else:
            event.day_label = f"{event.occurred_at.strftime('%B')} {event_date.day}"
        event.time_label = event.occurred_at.strftime("%I:%M %p").lstrip("0")
        template = titles.get(event.event_type, event.event_type.replace("_", " ").title())
        event_data = event.event_data if isinstance(event.event_data, dict) else {}
        event.timeline_title = template.format(
            source=_history_source_label(event.source),
            version_name=event_data.get("version_name") or "version",
        )
        event.timeline_description = event_data.get("modification_description")
        event.is_grants_version = event.event_type in {
            "grants_synopsis_version",
            "grants_forecast_version",
        }
        if event.is_grants_version:
            event.timeline_version_name = (
                event_data.get("version_name")
                or _history_field_label(event_data.get("history_type"))
                or "Version"
            )
            event.timeline_updated_label = _grants_updated_date_label(
                event_data.get("updated_date")
            )
            field_labels: list[str] = []
            for field in event_data.get("modified_fields") or []:
                if field in {
                    "revision",
                    "version",
                    "modComments",
                    "createTimeStamp",
                    "sendEmail",
                }:
                    continue
                field_label = _history_field_label(field)
                if field_label and field_label not in field_labels:
                    field_labels.append(field_label)
            event.timeline_modified_fields = field_labels
            event.timeline_source_revision = event_data.get("source_revision")
            event.timeline_version_type = _history_field_label(
                event_data.get("history_type")
            )
    return events


def _apply_type_tab(query, tab: str):
    """Filter using the same two-category BidLens type shown on Feed cards."""
    normalized_raw_type = func.lower(func.coalesce(Opportunity.opportunity_type, ""))
    is_rfi = or_(
        *(normalized_raw_type.like(f"%{indicator}%") for indicator in RFI_TYPE_INDICATORS)
    )
    return query.filter(is_rfi if tab == "rfi" else ~is_rfi)


def _stage_conditions():
    source = func.lower(func.coalesce(Opportunity.source, ""))
    raw_source_stage = func.lower(
        func.trim(
            func.coalesce(
                Opportunity.source_stage,
                Opportunity.opportunity_type,
                "",
            )
        )
    )
    normalized_raw_type = func.lower(func.coalesce(Opportunity.opportunity_type, ""))
    is_govwin = source.in_(("govwin_export", "govwin_api"))
    is_forecast = or_(
        and_(is_govwin, raw_source_stage == "forecast pre-rfp"),
        normalized_raw_type == "forecast",
    )
    is_rfi = or_(
        and_(is_govwin, raw_source_stage == "pre-rfp"),
        *(normalized_raw_type.like(f"%{indicator}%") for indicator in RFI_TYPE_INDICATORS),
    )
    return is_forecast, is_rfi


def _normalize_stage_filters(stages=None) -> tuple[str, ...]:
    if stages is None:
        return DISPLAY_STAGES
    if isinstance(stages, str):
        raw_values = stages.split(",") if stages else []
    else:
        raw_values = stages
    normalized = {
        str(value or "").strip().casefold()
        for value in raw_values
        if str(value or "").strip()
    }
    if "all" in normalized:
        return DISPLAY_STAGES
    return tuple(
        stage for stage in DISPLAY_STAGES if stage.casefold() in normalized
    )


def _apply_stage_filter(query, stages=None):
    selected = _normalize_stage_filters(stages)
    if selected == DISPLAY_STAGES:
        return query
    if not selected:
        return query.filter(Opportunity.id.is_(None))
    is_forecast, is_rfi = _stage_conditions()
    stage_conditions = {
        "Forecast": is_forecast,
        "RFI": and_(is_rfi, ~is_forecast),
        "RFP": and_(~is_forecast, ~is_rfi),
    }
    return query.filter(or_(*(stage_conditions[stage] for stage in selected)))


def _normalize_triage_source_filters(sources=None) -> tuple[str, ...]:
    if sources is None:
        return TRIAGE_SOURCE_FILTERS
    if isinstance(sources, str):
        raw_values = sources.split(",") if sources else []
    else:
        raw_values = sources
    aliases = {
        "sam.gov": "sam",
        "grants.gov": "grants",
        "grants_gov": "grants",
        "govwin_export": "govwin",
        "govwin_api": "govwin",
    }
    normalized = {
        aliases.get(
            str(value or "").strip().casefold(),
            str(value or "").strip().casefold(),
        )
        for value in raw_values
        if str(value or "").strip()
    }
    if "all" in normalized:
        return TRIAGE_SOURCE_FILTERS
    return tuple(source for source in TRIAGE_SOURCE_FILTERS if source in normalized)


def _apply_triage_source_filter(query, sources=None):
    selected = _normalize_triage_source_filters(sources)
    if selected == TRIAGE_SOURCE_FILTERS:
        return query
    if not selected:
        return query.filter(Opportunity.id.is_(None))

    normalized_source = func.lower(func.coalesce(Opportunity.source, ""))
    conditions = {
        "sam": normalized_source.in_(("sam", "sam.gov")),
        "grants": normalized_source.in_(("grants_gov", "grants.gov")),
        "govwin": normalized_source.in_(("govwin_export", "govwin_api")),
    }
    return query.filter(or_(*(conditions[source] for source in selected)))


def _exclude_inactive_govwin_stages(query):
    source = func.lower(func.coalesce(Opportunity.source, ""))
    raw_source_stage = func.lower(
        func.trim(
            func.coalesce(
                Opportunity.source_stage,
                Opportunity.opportunity_type,
                "",
            )
        )
    )
    return query.filter(
        ~and_(
            source.in_(("govwin_export", "govwin_api")),
            raw_source_stage == "source selection",
        )
    )


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
    if getattr(opp, "crm_pushed", False):
        return "Pushed to CRM"
    stage = (opp.review_stage or "").strip()
    if opp.decision_state == "SHORTLISTED" and stage:
        return f"Interested / {stage}"
    if opp.decision_state == "SHORTLISTED":
        return "Interested"
    if opp.decision_state == "ARCHIVED" and opp.archived_reason:
        return f"Archived / {opp.archived_reason}"
    return "Archived" if opp.decision_state == "ARCHIVED" else "Active"


def _base_export_filename(view: str, tab: str) -> str:
    safe_view = (view or "feed").replace("-", "_")
    if safe_view == "shortlist":
        safe_view = "team_interested"
    elif safe_view == "my_shortlist":
        safe_view = "my_interested"
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
    direction: str = "desc",
    date_type: str = DATE_TYPE_IMPORTED,
    date_filter: str = "",
    date_from: str = "",
    date_to: str = "",
    show_passed: str = "",
    show_past_due: str = "",
    lane_id: str | None = None,
    search: str = "",
    stages=None,
):
    if view == "shortlist":
        q = _team_interest_query(db, user, tab)
        q = _apply_past_due_filter(q, show_past_due=show_past_due)
    elif view == "archive":
        q = _opp_list_query(db, user, "ARCHIVED", tab)
    elif view == "my_shortlist":
        q = _my_shortlist_query(db, user, tab)
        q = _apply_feed_search(q, search_term=search)
        q = _apply_lane_filter(q, db, user, lane_id=lane_id)
        q = _apply_stage_filter(q, stages)
    else:
        q = _feed_query(db, user)
        q = apply_org_filters(q, db, user)
        q = _apply_lane_filter(q, db, user, lane_id=lane_id)
        q = _apply_feed_search(q, search_term=search)
        q = _apply_past_due_filter(q)
        q = _apply_stage_filter(q, stages)

    return q


def _vote_export_maps(db: Session, org_id: int, opp_ids: list[int]) -> tuple[dict[int, dict[str, int]], dict[int, list[str]], dict[int, list[str]]]:
    if not opp_ids:
        return {}, {}, {}

    counts = get_vote_counts(db, opp_ids)
    interest_users, pass_users = get_vote_user_maps(db, org_id=org_id, opp_ids=opp_ids)
    return counts, interest_users, pass_users


def _sort_export_opportunities(
    db: Session,
    opportunities: list[Opportunity],
    *,
    view: str,
    sort: str,
    direction: str = "desc",
    date_type: str = DATE_TYPE_IMPORTED,
) -> list[Opportunity]:
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
    elif view in {"feed", "my_shortlist"}:
        sort = _normalize_feed_sort(sort)
        direction = _normalize_sort_direction(direction)

        def feed_sort_value(opp):
            if sort == "due":
                return opp.response_deadline
            if sort == "agency":
                return (opp.agency or "").casefold() or None
            if sort == "title":
                return (opp.title or "").casefold() or None
            return opp.upserted_at

        populated = [opp for opp in opportunities if feed_sort_value(opp) is not None]
        missing = [opp for opp in opportunities if feed_sort_value(opp) is None]
        populated.sort(
            key=lambda opp: (feed_sort_value(opp), opp.id),
            reverse=direction == "desc",
        )
        opportunities = populated + missing
    else:
        opportunities.sort(key=lambda opp: (opp.response_deadline or date.max, opp.id))

    return opportunities


def _team_interest_label(*, total: int, current_user_interested: bool) -> str:
    teammate_count = max(0, total - (1 if current_user_interested else 0))
    if current_user_interested:
        if teammate_count == 0:
            return "Only you so far"
        noun = "teammate" if teammate_count == 1 else "teammates"
        return f"You + {teammate_count} {noun}"
    if teammate_count == 0:
        return "No team interest yet"
    noun = "teammate" if teammate_count == 1 else "teammates"
    return f"{teammate_count} {noun} interested"


def _normalized_opportunity_type(opportunity: Opportunity) -> str:
    """Return the BidLens display stage while preserving the source values."""
    return normalize_display_stage(
        source=opportunity.source,
        opportunity_type=opportunity.opportunity_type,
        source_stage=opportunity.source_stage,
    )


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
    lane_rows = (
        db.query(OpportunityPursuitLaneMatch, PursuitLane)
        .join(PursuitLane, PursuitLane.id == OpportunityPursuitLaneMatch.pursuit_lane_id)
        .filter(
            OpportunityPursuitLaneMatch.organization_id == _user_org_id(user),
            OpportunityPursuitLaneMatch.opportunity_id.in_(opp_ids),
            PursuitLane.organization_id == _user_org_id(user),
            PursuitLane.is_active.is_(True),
        )
        .order_by(PursuitLane.name.asc())
        .all()
        if opp_ids else []
    )
    lane_map: dict[int, list[dict]] = {}
    for match, lane in lane_rows:
        lane_map.setdefault(match.opportunity_id, []).append(
            {
                "id": lane.id,
                "name": lane.name,
                "reasons": match.matched_reasons or [],
            }
        )
    crm_user_ids = sorted({opp.crm_pushed_by for opp in opportunities if getattr(opp, "crm_pushed_by", None)})
    crm_user_map = {
        user_id: (name or email or f"User {user_id}")
        for user_id, name, email in (
            db.query(User.id, User.name, User.email)
            .filter(User.id.in_(crm_user_ids))
            .all()
            if crm_user_ids else []
        )
    }
    current_user_label = (user.name or user.email or "").strip()

    for opp in opportunities:
        c = counts.get(opp.id, {"pursue": 0, "pass": 0})
        opp.pursue_count = c["pursue"]
        opp.pass_count = c["pass"]
        opp.user_vote = user_votes.get(opp.id)
        opp.pursue_users = pursue_users_map.get(opp.id, [])
        opp.pass_users = pass_users_map.get(opp.id, [])
        opp.current_user_interested = opp.user_vote == "PURSUE"
        opp.teammate_interest_count = max(
            0,
            opp.pursue_count - (1 if opp.current_user_interested else 0),
        )
        opp.teammate_interest_users = [
            display_name
            for display_name in opp.pursue_users
            if not (opp.current_user_interested and display_name == current_user_label)
        ]
        opp.team_interest_label = _team_interest_label(
            total=opp.pursue_count,
            current_user_interested=opp.current_user_interested,
        )
        opp.normalized_opportunity_type = _normalized_opportunity_type(opp)
        opp.pursuit_lanes = lane_map.get(opp.id, [])
        opp.crm_pushed_by_current_user = bool(getattr(opp, "crm_pushed", False) and opp.crm_pushed_by == user.id)
        opp.crm_pushed_by_label = crm_user_map.get(getattr(opp, "crm_pushed_by", None))
        opp.preview_description = _clean_preview_text(opp.description_text or opp.description or "")
        opp.preview_has_sam_fallback = bool((not opp.preview_description) and (getattr(opp, "source_url", None) or getattr(opp, "sam_url", None)))

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
                UserOpportunity.organization_id == _user_org_id(user),
            ),
        )
        .filter(Opportunity.decision_state == decision_state)
        .filter(Opportunity.organization_id == _user_org_id(user))
    )
    q = _apply_type_tab(q, tab)
    return q


def _feed_query(db: Session, user, tab: str | None = None):
    """Main feed: all organization opportunities the current user has not reviewed.

    ``tab`` remains accepted for legacy callers and URLs, but opportunity type
    is descriptive metadata rather than a Feed partition.
    """
    return build_feed_query(
        db,
        organization_id=_user_org_id(user),
        user_id=user.id,
    )


def _user_archive_query(db: Session, user, tab: str):
    """Qualified opportunities archived by the current user through PASS."""
    q = (
        db.query(Opportunity, UserOpportunity.watched.label("watched"))
        .join(
            Vote,
            and_(
                Vote.opp_id == Opportunity.id,
                Vote.org_id == _user_org_id(user),
                Vote.user_id == user.id,
                Vote.vote == "PASS",
            ),
        )
        .outerjoin(
            UserOpportunity,
            and_(
                UserOpportunity.opportunity_id == Opportunity.id,
                UserOpportunity.user_id == user.id,
                UserOpportunity.organization_id == _user_org_id(user),
            ),
        )
        .filter(Opportunity.organization_id == _user_org_id(user))
        .filter(Opportunity.decision_state != "ARCHIVED")
        .filter(Opportunity.qualification_status == QUALIFICATION_QUALIFIED)
    )
    return _apply_type_tab(q, tab)


def _team_interest_query(db: Session, user, tab: str):
    """Organization opportunities with at least one user's interest signal."""
    interested_opp_ids = (
        select(Vote.opp_id)
        .where(
            Vote.org_id == _user_org_id(user),
            Vote.vote == "PURSUE",
        )
    )
    q = (
        db.query(Opportunity, UserOpportunity.watched.label("watched"))
        .outerjoin(
            UserOpportunity,
            and_(
                UserOpportunity.opportunity_id == Opportunity.id,
                UserOpportunity.user_id == user.id,
                UserOpportunity.organization_id == _user_org_id(user),
            ),
        )
        .filter(Opportunity.organization_id == _user_org_id(user))
        .filter(Opportunity.decision_state != "ARCHIVED")
        .filter(Opportunity.qualification_status == QUALIFICATION_QUALIFIED)
        .filter(Opportunity.id.in_(interested_opp_ids))
    )
    q = _apply_type_tab(q, tab)
    return q


def _my_shortlist_query(db: Session, user, tab: str):
    """User's personal interested list.

    ``tab`` remains accepted for legacy links, but stages are now controlled by
    the same multi-select filter used by the Feed.
    """
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
                UserOpportunity.organization_id == _user_org_id(user),
            ),
        )
        .filter(Opportunity.decision_state != "ARCHIVED")
        .filter(Opportunity.organization_id == _user_org_id(user))
        .filter(Opportunity.qualification_status == QUALIFICATION_QUALIFIED)
    )
    return q


def _best_description_text(opportunity: Opportunity) -> str:
    description_text = (opportunity.description_text or "").strip()
    if description_text:
        return description_text

    description = (opportunity.description or "").strip()
    if description and not _is_url_like(description):
        return description

    return ""


def _raw_path(payload: dict, *path: str):
    value = payload
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
        if value in (None, ""):
            return None
    return value


def _first_raw_value(payload: dict, *paths: tuple[str, ...]):
    for path in paths:
        value = _raw_path(payload, *path)
        if value not in (None, ""):
            return value
    return None


def _format_grants_gov_date(value) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    candidates = [text]
    if "," in text and " " in text:
        candidates.append(text.rsplit(" ", 1)[0])
    for candidate in candidates:
        for fmt in (
            "%m/%d/%Y",
            "%Y-%m-%d",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d-%H-%M-%S",
            "%b %d, %Y %I:%M:%S %p",
        ):
            for candidate_value in (candidate, candidate[:19], candidate[:10]):
                try:
                    parsed = datetime.strptime(candidate_value, fmt).date()
                    return parsed.strftime("%B %-d, %Y")
                except ValueError:
                    pass
    return text


def _normalize_grants_gov_status(value) -> str | None:
    if not value:
        return None
    normalized = str(value).strip().lower()
    status_map = {
        "forecast": "Forecast",
        "forecasted": "Forecast",
        "posted": "Open",
        "synopsis": "Open",
        "open": "Open",
        "closed": "Closed",
        "archived": "Archived",
    }
    return status_map.get(normalized, str(value).strip().title())


def _grants_gov_detail_metadata(opportunity: Opportunity) -> dict[str, str | None]:
    if opportunity.source != "grants_gov" or not isinstance(opportunity.raw_source_payload, dict):
        return {}

    raw = opportunity.raw_source_payload
    status = _normalize_grants_gov_status(
        _first_raw_value(
            raw,
            ("oppStatus",),
            ("search_result", "oppStatus"),
            ("ost",),
            ("detail_payload", "data", "ost"),
            ("docType",),
            ("search_result", "docType"),
        )
    )

    doc_type = str(
        _first_raw_value(
            raw,
            ("docType",),
            ("search_result", "docType"),
            ("detail_payload", "data", "docType"),
        )
        or ""
    ).lower()
    version_value = _first_raw_value(
        raw,
        ("forecast", "version"),
        ("detail_payload", "data", "forecast", "version"),
        ("synopsis", "version"),
        ("detail_payload", "data", "synopsis", "version"),
        ("version",),
        ("revision",),
        ("detail_payload", "data", "revision"),
    )
    version = None
    if version_value not in (None, ""):
        if doc_type == "forecast" or status == "Forecast":
            version = f"Forecast {version_value}"
        else:
            version = f"Version {version_value}"

    last_updated = _format_grants_gov_date(
        _first_raw_value(
            raw,
            ("forecast", "lastUpdatedDate"),
            ("detail_payload", "data", "forecast", "lastUpdatedDate"),
            ("synopsis", "lastUpdatedDate"),
            ("detail_payload", "data", "synopsis", "lastUpdatedDate"),
            ("lastUpdatedDate",),
            ("detail_payload", "data", "lastUpdatedDate"),
            ("search_result", "lastUpdatedDate"),
            ("updatedDate",),
            ("updateDate",),
        )
    )

    return {
        "source": "Grants.gov",
        "status": status,
        "version": version,
        "last_updated": last_updated,
    }


def _format_file_size(value) -> str | None:
    if value in (None, ""):
        return None
    try:
        size = int(value)
    except (TypeError, ValueError):
        return str(value)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "bytes":
                return f"{size} {unit}"
            return f"{size:.1f} {unit}"
        size = size / 1024
    return str(value)


def _coerce_document_url(value) -> dict[str, str | None] | None:
    if isinstance(value, str):
        text = value.strip()
        return {"label": text, "url": text} if text else None
    if not isinstance(value, dict):
        return None
    url = (
        value.get("url")
        or value.get("URL")
        or value.get("link")
        or value.get("href")
        or value.get("documentUrl")
        or value.get("documentURL")
    )
    label = (
        value.get("label")
        or value.get("name")
        or value.get("title")
        or value.get("description")
        or value.get("fileName")
        or url
    )
    if not url and not label:
        return None
    return {"label": str(label).strip() if label else None, "url": str(url).strip() if url else None}


def _grants_gov_attachment_download_url(attachment_id) -> str | None:
    if attachment_id in (None, ""):
        return None
    return f"https://www.grants.gov/grantsws/rest/opportunity/att/download/{attachment_id}"


def _grants_gov_document_metadata(opportunity: Opportunity) -> dict:
    if opportunity.source != "grants_gov" or not isinstance(opportunity.raw_source_payload, dict):
        return {"folders": [], "document_urls": []}

    raw = opportunity.raw_source_payload
    folders = _first_raw_value(
        raw,
        ("synopsisAttachmentFolders",),
        ("detail_payload", "data", "synopsisAttachmentFolders"),
    )
    normalized_folders = []
    if isinstance(folders, list):
        for folder in folders:
            if not isinstance(folder, dict):
                continue
            attachments = []
            for attachment in folder.get("synopsisAttachments") or []:
                if not isinstance(attachment, dict):
                    continue
                attachment_id = attachment.get("id")
                file_name = attachment.get("fileName")
                file_description = attachment.get("fileDescription")
                mime_type = attachment.get("mimeType")
                file_size = _format_file_size(attachment.get("fileLobSize"))
                if any((attachment_id, file_name, file_description, mime_type, file_size)):
                    attachments.append(
                        {
                            "id": attachment_id,
                            "file_name": file_name,
                            "file_description": file_description,
                            "mime_type": mime_type,
                            "file_size": file_size,
                            "download_url": _grants_gov_attachment_download_url(attachment_id),
                        }
                    )
            if attachments:
                normalized_folders.append(
                    {
                        "folder_name": folder.get("folderName"),
                        "folder_type": folder.get("folderType"),
                        "zip_size": _format_file_size(folder.get("zipLobSize")),
                        "attachments": attachments,
                    }
                )

    document_urls = _first_raw_value(
        raw,
        ("synopsisDocumentURLs",),
        ("detail_payload", "data", "synopsisDocumentURLs"),
    )
    normalized_urls = []
    if isinstance(document_urls, list):
        for document_url in document_urls:
            normalized = _coerce_document_url(document_url)
            if normalized:
                normalized_urls.append(normalized)

    return {"folders": normalized_folders, "document_urls": normalized_urls}


def _clean_preview_text(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(value))
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _apply_past_due_filter(query, *, show_past_due: str = ""):
    return exclude_past_due_opportunities(query, show_past_due=show_past_due)


def _active_lanes(db: Session, user) -> list[PursuitLane]:
    return (
        db.query(PursuitLane)
        .filter(
            PursuitLane.organization_id == _user_org_id(user),
            PursuitLane.is_active.is_(True),
        )
        .order_by(PursuitLane.name.asc())
        .all()
    )


def _apply_lane_filter(query, db: Session, user, *, lane_id: str | int | None = None):
    if not lane_id:
        return query

    org_id = _user_org_id(user)
    if str(lane_id) == "my_lanes":
        my_lane_ids = [
            lane.id
            for lane in user_my_lanes(db, organization_id=org_id, user_id=user.id)
        ]
        if not my_lane_ids:
            return query.filter(False)
        matched_opp_ids = (
            select(OpportunityPursuitLaneMatch.opportunity_id)
            .where(
                OpportunityPursuitLaneMatch.organization_id == org_id,
                OpportunityPursuitLaneMatch.pursuit_lane_id.in_(my_lane_ids),
            )
        )
        return query.filter(Opportunity.id.in_(matched_opp_ids))

    try:
        selected_lane_id = int(lane_id)
    except (TypeError, ValueError):
        return query

    lane = (
        db.query(PursuitLane.id)
        .filter(
            PursuitLane.id == selected_lane_id,
            PursuitLane.organization_id == org_id,
            PursuitLane.is_active.is_(True),
        )
        .first()
    )
    if not lane:
        return query

    matched_opp_ids = (
        select(OpportunityPursuitLaneMatch.opportunity_id)
        .where(
            OpportunityPursuitLaneMatch.organization_id == org_id,
            OpportunityPursuitLaneMatch.pursuit_lane_id == selected_lane_id,
        )
    )
    return query.filter(Opportunity.id.in_(matched_opp_ids))


def _apply_feed_search(query, *, search_term: str = ""):
    term = (search_term or "").strip()
    if not term:
        return query

    pattern = f"%{term}%"
    return query.filter(
        or_(
            Opportunity.title.ilike(pattern),
            Opportunity.agency.ilike(pattern),
            Opportunity.solicitation_number.ilike(pattern),
            Opportunity.sam_notice_id.ilike(pattern),
            Opportunity.source_record_id.ilike(pattern),
            Opportunity.naics.ilike(pattern),
            Opportunity.naics_title.ilike(pattern),
            Opportunity.description.ilike(pattern),
            Opportunity.description_text.ilike(pattern),
        )
    )


def _normalize_date_type(date_type: str = "", *, sort: str = "") -> str:
    if date_type in DATE_TYPES:
        return date_type
    if sort == "deadline":
        return DATE_TYPE_DUE
    if sort == "posted":
        return DATE_TYPE_POSTED
    return DATE_TYPE_IMPORTED


def _date_field_for_type(date_type: str):
    date_type = _normalize_date_type(date_type)
    if date_type == DATE_TYPE_DUE:
        return Opportunity.response_deadline
    if date_type == DATE_TYPE_POSTED:
        return Opportunity.posted_date
    return Opportunity.upserted_at


def _apply_date_window_filter(query, *, date_type: str = DATE_TYPE_IMPORTED, date_filter: str = ""):
    today = date.today()
    date_field = _date_field_for_type(date_type)
    if date_filter == "today":
        return query.filter(func.date(date_field) == today)
    if date_filter == "7d":
        if _normalize_date_type(date_type) == DATE_TYPE_DUE:
            return query.filter(func.date(date_field) >= today, func.date(date_field) <= today + timedelta(days=7))
        return query.filter(func.date(date_field) >= today - timedelta(days=7))
    if date_filter == "30d":
        if _normalize_date_type(date_type) == DATE_TYPE_DUE:
            return query.filter(func.date(date_field) >= today, func.date(date_field) <= today + timedelta(days=30))
        return query.filter(func.date(date_field) >= today - timedelta(days=30))
    return query


def _apply_date_ordering(query, *, date_type: str = DATE_TYPE_IMPORTED):
    date_type = _normalize_date_type(date_type)
    date_field = _date_field_for_type(date_type)
    if date_type == DATE_TYPE_DUE:
        return query.order_by(date_field.asc(), Opportunity.id.desc())
    return query.order_by(date_field.desc(), Opportunity.id.desc())


def _normalize_feed_sort(sort: str = "") -> str:
    aliases = {
        "newest": "imported",
        "deadline": "due",
    }
    normalized = aliases.get(sort, sort)
    return normalized if normalized in {"imported", "due", "agency", "title"} else "imported"


def _normalize_sort_direction(direction: str = "") -> str:
    return "asc" if direction == "asc" else "desc"


def _pagination_values(result_count: int, page: int, page_size: int) -> tuple[int, int, int]:
    total_pages = max(1, (result_count + page_size - 1) // page_size)
    current_page = min(max(1, page), total_pages)
    return current_page, total_pages, (current_page - 1) * page_size


def _apply_feed_ordering(query, *, sort: str = "imported", direction: str = "desc"):
    sort = _normalize_feed_sort(sort)
    direction = _normalize_sort_direction(direction)
    sort_field = {
        "due": Opportunity.response_deadline,
        "agency": func.lower(Opportunity.agency),
        "title": func.lower(Opportunity.title),
    }.get(sort, Opportunity.upserted_at)
    ordered_field = sort_field.asc() if direction == "asc" else sort_field.desc()
    ordered_id = Opportunity.id.asc() if direction == "asc" else Opportunity.id.desc()
    return query.order_by(sort_field.is_(None).asc(), ordered_field, ordered_id)


def _queue_counts(db: Session, user, tab: str) -> dict[str, int]:
    org_id = _user_org_id(user)
    base = (
        db.query(Opportunity.id)
        .filter(Opportunity.organization_id == org_id)
        .filter(Opportunity.decision_state != "ARCHIVED")
        .filter(Opportunity.qualification_status == QUALIFICATION_QUALIFIED)
    )
    base = _apply_type_tab(base, tab)

    interested_opp_ids = (
        select(Vote.opp_id)
        .where(Vote.org_id == org_id, Vote.user_id == user.id, Vote.vote == "PURSUE")
    )
    passed_opp_ids = (
        select(Vote.opp_id)
        .where(Vote.org_id == org_id, Vote.user_id == user.id, Vote.vote == "PASS")
    )

    new_count = (
        base
        .filter(~Opportunity.id.in_(interested_opp_ids))
        .filter(~Opportunity.id.in_(passed_opp_ids))
        .count()
    )
    my_interested_count_query = (
        db.query(func.count(func.distinct(Opportunity.id)))
        .join(
            Vote,
            and_(
                Vote.opp_id == Opportunity.id,
                Vote.org_id == org_id,
                Vote.user_id == user.id,
                Vote.vote == "PURSUE",
            ),
        )
        .filter(Opportunity.organization_id == org_id)
        .filter(Opportunity.decision_state != "ARCHIVED")
        .filter(Opportunity.qualification_status == QUALIFICATION_QUALIFIED)
    )
    my_interested_count = _apply_type_tab(my_interested_count_query, tab).scalar() or 0
    passed_count = (
        db.query(func.count(Vote.id))
        .join(Opportunity, Opportunity.id == Vote.opp_id)
        .filter(
            Vote.org_id == org_id,
            Vote.user_id == user.id,
            Vote.vote == "PASS",
            Opportunity.organization_id == org_id,
            Opportunity.decision_state != "ARCHIVED",
            Opportunity.qualification_status == QUALIFICATION_QUALIFIED,
        )
    )
    passed_count = _apply_type_tab(passed_count, tab).scalar() or 0
    return {
        "new": new_count,
        "my_interested": my_interested_count,
        "passed": passed_count,
    }


def _triage_counts(db: Session, user) -> dict[str, int]:
    org_id = _user_org_id(user)
    rows = (
        db.query(Opportunity.qualification_status, func.count(Opportunity.id))
        .filter(Opportunity.organization_id == org_id)
        .group_by(Opportunity.qualification_status)
        .all()
    )
    counts = {status: 0 for status in QUALIFICATION_STATUSES}
    for status, count in rows:
        if status in counts:
            counts[status] = count or 0
    counts["all"] = sum(counts.values())
    return counts


# ── Feed (INBOX) ──────────────────────────────────────────────

@router.get("/")
async def feed(
    request: Request,
    tab: str = "solicitations",
    sort: str = "imported",
    direction: str = "desc",
    date_type: str = "",
    date_filter: str = "",
    date_from: str = "",
    date_to: str = "",
    show_passed: str = "",
    show_past_due: str = "",
    lane_id: str | None = None,
    q: str = "",
    stages: str | None = None,
    stage: str | None = None,
    page: int = 1,
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    pre_live_redirect = _pre_live_product_redirect(db, user)
    if pre_live_redirect:
        return pre_live_redirect

    search_query = q
    selected_sort = _normalize_feed_sort(sort)
    selected_direction = _normalize_sort_direction(direction)
    # Legacy date/pass parameters remain accepted for old links, but the Feed
    # always represents the current user's active queue.
    query = feed_awaiting_review_query(
        db,
        organization_id=_user_org_id(user),
        user_id=user.id,
    )
    query = _apply_lane_filter(query, db, user, lane_id=lane_id)
    query = _apply_feed_search(query, search_term=search_query)
    selected_stages = _normalize_stage_filters(
        stages if stages is not None else ([stage] if stage and stage != "All" else None)
    )
    query = _apply_stage_filter(query, selected_stages)
    query = _apply_feed_ordering(query, sort=selected_sort, direction=selected_direction)

    result_count = query.count()
    current_page, total_pages, offset = _pagination_values(
        result_count,
        page,
        FEED_PAGE_SIZE,
    )
    rows = query.offset(offset).limit(FEED_PAGE_SIZE).all()

    return templates.TemplateResponse("feed.html", {
        "request": request,
        "user": user,
        "opportunities": _enrich_opps(rows, db, user),
        "active_page": "feed",
        "sidebar": get_sidebar(db, user),
        "sort": selected_sort,
        "direction": selected_direction,
        "lane_id": lane_id,
        "q": search_query,
        "selected_stages": selected_stages,
        "stages_value": ",".join(selected_stages),
        "result_count": result_count,
        "page": current_page,
        "page_size": FEED_PAGE_SIZE,
        "total_pages": total_pages,
        "active_lanes": _active_lanes(db, user),
        "my_lanes": user_my_lanes(db, organization_id=_user_org_id(user), user_id=user.id),
        "triage_enabled": user.triage_enabled,
        "now": datetime.utcnow(),
    })


@router.get("/triage")
async def triage_queue(
    request: Request,
    date_type: str = "",
    sort: str = "imported",
    direction: str = "desc",
    date_filter: str = "",
    q: str = "",
    stages: str | None = None,
    sources: str | None = None,
    stage: str | None = None,
    page: int = 1,
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    pre_live_redirect = _pre_live_product_redirect(db, user)
    if pre_live_redirect:
        return pre_live_redirect
    if not _is_admin(user):
        return RedirectResponse(url="/", status_code=303)

    query = (
        db.query(Opportunity, UserOpportunity.watched.label("watched"))
        .outerjoin(
            UserOpportunity,
            and_(
                UserOpportunity.opportunity_id == Opportunity.id,
                UserOpportunity.user_id == user.id,
                UserOpportunity.organization_id == _user_org_id(user),
            ),
        )
        .filter(Opportunity.organization_id == _user_org_id(user))
        .filter(Opportunity.decision_state != "ARCHIVED")
        .filter(Opportunity.qualification_status == QUALIFICATION_UNREVIEWED)
    )
    query = _exclude_inactive_govwin_stages(query)
    selected_sort = _normalize_feed_sort(sort)
    selected_direction = _normalize_sort_direction(direction)
    selected_stages = _normalize_stage_filters(
        stages if stages is not None else ([stage] if stage and stage != "All" else None)
    )
    selected_sources = _normalize_triage_source_filters(sources)
    query = _apply_feed_search(query, search_term=q)
    query = _apply_stage_filter(query, selected_stages)
    query = _apply_triage_source_filter(query, selected_sources)
    query = _apply_feed_ordering(query, sort=selected_sort, direction=selected_direction)
    result_count = query.count()
    current_page, total_pages, offset = _pagination_values(
        result_count,
        page,
        TRIAGE_PAGE_SIZE,
    )
    rows = query.offset(offset).limit(TRIAGE_PAGE_SIZE).all()

    return templates.TemplateResponse("triage.html", {
        "request": request,
        "user": user,
        "opportunities": _enrich_opps(rows, db, user),
        "active_page": "triage",
        "triage_enabled": user.triage_enabled,
        "sort": selected_sort,
        "direction": selected_direction,
        "q": q,
        "selected_stages": selected_stages,
        "stages_value": ",".join(selected_stages),
        "source_options": TRIAGE_SOURCE_OPTIONS,
        "selected_sources": selected_sources,
        "sources_value": ",".join(selected_sources),
        "result_count": result_count,
        "page": current_page,
        "page_size": TRIAGE_PAGE_SIZE,
        "total_pages": total_pages,
        "now": datetime.utcnow(),
    })


@router.get("/intake")
async def intake_redirect():
    return RedirectResponse(url="/triage", status_code=303)


REVIEW_STAGES = ["Team Review", "Director Review", "Approved"]


# ── Team Interest ────────────────────────────────────────────

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
    pre_live_redirect = _pre_live_product_redirect(db, user)
    if pre_live_redirect:
        return pre_live_redirect

    q = _team_interest_query(db, user, tab)
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


# ── My Interest ──────────────────────────────────────────────

@router.get("/my-shortlist")
async def my_shortlist(
    request: Request,
    tab: str = "solicitations",
    sort: str = "imported",
    direction: str = "desc",
    q: str = "",
    stages: str | None = None,
    stage: str | None = None,
    lane_id: str | None = None,
    page: int = 1,
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    pre_live_redirect = _pre_live_product_redirect(db, user)
    if pre_live_redirect:
        return pre_live_redirect

    selected_sort = _normalize_feed_sort(sort)
    selected_direction = _normalize_sort_direction(direction)
    selected_stages = _normalize_stage_filters(
        stages if stages is not None else ([stage] if stage and stage != "All" else None)
    )
    query = _my_shortlist_query(db, user, tab)
    query = _apply_feed_search(query, search_term=q)
    query = _apply_lane_filter(query, db, user, lane_id=lane_id)
    query = _apply_stage_filter(query, selected_stages)
    query = _apply_feed_ordering(query, sort=selected_sort, direction=selected_direction)
    result_count = query.count()
    current_page, total_pages, offset = _pagination_values(
        result_count,
        page,
        FEED_PAGE_SIZE,
    )
    rows = query.offset(offset).limit(FEED_PAGE_SIZE).all()

    opps = _enrich_opps(rows, db, user)

    return templates.TemplateResponse("my_shortlist.html", {
        "request": request,
        "user": user,
        "opportunities": opps,
        "current_tab": tab,
        "active_page": "my_shortlist",
        "sidebar": get_sidebar(db, user),
        "sort": selected_sort,
        "direction": selected_direction,
        "lane_id": lane_id,
        "q": q,
        "selected_stages": selected_stages,
        "stages_value": ",".join(selected_stages),
        "result_count": result_count,
        "page": current_page,
        "page_size": FEED_PAGE_SIZE,
        "total_pages": total_pages,
        "active_lanes": _active_lanes(db, user),
        "my_lanes": user_my_lanes(db, organization_id=_user_org_id(user), user_id=user.id),
    })


# ── Personal Archive (PASS) ──────────────────────────────────

@router.get("/archive")
async def archive(
    request: Request,
    tab: str = "solicitations",
    sort: str = "imported",
    direction: str = "desc",
    q: str = "",
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    pre_live_redirect = _pre_live_product_redirect(db, user)
    if pre_live_redirect:
        return pre_live_redirect

    selected_sort = _normalize_feed_sort(sort)
    selected_direction = _normalize_sort_direction(direction)
    query = _user_archive_query(db, user, tab)
    query = _apply_feed_search(query, search_term=q)
    query = _apply_feed_ordering(
        query,
        sort=selected_sort,
        direction=selected_direction,
    )
    result_count = query.count()
    rows = query.limit(50).all()

    return templates.TemplateResponse("archive.html", {
        "request": request,
        "user": user,
        "opportunities": _enrich_opps(rows, db, user),
        "current_tab": tab,
        "active_page": "archive",
        "sort": selected_sort,
        "direction": selected_direction,
        "q": q,
        "result_count": result_count,
        "now": datetime.utcnow(),
    })


@router.get("/opportunities/export.csv")
async def export_opportunities_csv(
    request: Request,
    view: str = "feed",
    tab: str = "solicitations",
    sort: str = "newest",
    direction: str = "desc",
    date_type: str = "",
    date_filter: str = "",
    date_from: str = "",
    date_to: str = "",
    show_passed: str = "",
    show_past_due: str = "",
    lane_id: str | None = None,
    q: str = "",
    stages: str | None = None,
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
        direction=direction,
        date_type=date_type,
        date_filter=date_filter,
        date_from=date_from,
        date_to=date_to,
        show_passed=show_passed,
        show_past_due=show_past_due,
        lane_id=lane_id,
        search=q,
        stages=stages,
    )

    rows = q.all()
    opportunities = _enrich_opps(rows, db, user)
    opportunities = _sort_export_opportunities(
        db,
        opportunities,
        view=view,
        sort=sort,
        direction=direction,
        date_type=date_type,
    )
    opp_ids = [opp.id for opp in opportunities]

    vote_counts, interest_users_map, pass_users_map = _vote_export_maps(db, _user_org_id(user), opp_ids)
    user_votes = get_user_votes(db, user.id, opp_ids)

    user_opp_rows = (
        db.query(UserOpportunity)
        .filter(
            UserOpportunity.organization_id == _user_org_id(user),
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
        "NAICS Title",
        "Set-Aside",
        "Account Type",
        "Account Type Confidence",
        "Account Type Source",
        "Solicitation Number",
        "Source",
        "Source Record ID",
        "External Source Key",
        "SAM Notice ID",
        "Source URL",
        "Current Org-Level Status",
        "Interested Count",
        "Archived By Count",
        "Users Who Are Interested",
        "Users Who Archived",
        "Current User Vote",
        "Watched",
        "My Interested",
        "Internal Deadline",
        "Notes",
    ])

    today = date.today()
    for opp in opportunities:
        department, sub_agency = _agency_parts_for_export(opp.agency)
        counts = vote_counts.get(opp.id, {"pursue": getattr(opp, "pursue_count", 0), "pass": getattr(opp, "pass_count", 0)})
        interest_users = "; ".join(interest_users_map.get(opp.id, []))
        pass_users = "; ".join(pass_users_map.get(opp.id, []))
        user_opp = user_opp_map.get(opp.id)
        current_vote = user_votes.get(opp.id) or getattr(opp, "user_vote", None) or ""
        current_vote_label = "Interested" if current_vote == "PURSUE" else "Archive" if current_vote == "PASS" else ""
        my_interest = "Yes" if current_vote == "PURSUE" else "No"
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
            opp.naics_title or "",
            opp.set_aside or "",
            opp.account_type or "",
            opp.account_type_confidence or "",
            opp.account_type_source or "",
            opp.solicitation_number or "",
            opp.source or "",
            opp.source_record_id or "",
            opp.external_source_key or "",
            opp.sam_notice_id or "",
            opp.source_url or opp.sam_url or "",
            _current_org_status(opp),
            counts.get("pursue", 0),
            counts.get("pass", 0),
            interest_users,
            pass_users,
            current_vote_label,
            watched,
            my_interest,
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
    if opportunity.qualification_status != QUALIFICATION_QUALIFIED and not _is_admin(user):
        return RedirectResponse(url="/", status_code=303)

    resolved_description = _best_description_text(opportunity)
    raw_payload = opportunity.raw_source_payload if isinstance(opportunity.raw_source_payload, dict) else {}
    needs_grants_gov_detail = opportunity.source == "grants_gov" and (
        not resolved_description or not raw_payload.get("detail_payload")
    )
    if needs_grants_gov_detail:
        try:
            if enrich_grants_gov_opportunity_detail(db, opportunity):
                resolved_description = _best_description_text(opportunity)
        except (GrantsGovApiError, requests.RequestException) as exc:
            db.rollback()
            logger.warning(
                "Grants.gov detail enrichment failed opportunity_id=%s source_record_id=%s error=%s",
                opportunity.id,
                opportunity.source_record_id,
                exc,
            )
    resolved_description = resolved_description or None
    grants_gov_metadata = _grants_gov_detail_metadata(opportunity)
    grants_gov_documents = _grants_gov_document_metadata(opportunity)

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
    user_vote = user_votes.get(opp_id)
    pursue_users_map, _ = get_vote_user_maps(
        db,
        org_id=_user_org_id(user),
        opp_ids=[opp_id],
    )
    pursue_users = pursue_users_map.get(opp_id, [])
    current_user_interested = user_vote == "PURSUE"
    current_user_label = (user.name or user.email or "").strip()
    teammate_interest_users = [
        display_name
        for display_name in pursue_users
        if not (current_user_interested and display_name == current_user_label)
    ]
    opportunity.crm_pushed_by_current_user = bool(opportunity.crm_pushed and opportunity.crm_pushed_by == user.id)
    opportunity.crm_pushed_by_label = None
    if opportunity.crm_pushed_by:
        crm_user = db.query(User).filter(User.id == opportunity.crm_pushed_by).first()
        if crm_user:
            opportunity.crm_pushed_by_label = crm_user.name or crm_user.email
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
    pursuit_lanes = [
        {
            "id": lane.id,
            "name": lane.name,
            "reasons": match.matched_reasons or [],
        }
        for match, lane in (
            db.query(OpportunityPursuitLaneMatch, PursuitLane)
            .join(
                PursuitLane,
                PursuitLane.id == OpportunityPursuitLaneMatch.pursuit_lane_id,
            )
            .filter(
                OpportunityPursuitLaneMatch.organization_id == _user_org_id(user),
                OpportunityPursuitLaneMatch.opportunity_id == opportunity.id,
                PursuitLane.organization_id == _user_org_id(user),
                PursuitLane.is_active.is_(True),
            )
            .order_by(PursuitLane.name.asc())
            .all()
        )
    ]
    history_events = _prepare_history_events(
        (
            db.query(OpportunityHistoryEvent)
            .filter(
                OpportunityHistoryEvent.organization_id == _user_org_id(user),
                OpportunityHistoryEvent.opportunity_id == opportunity.id,
            )
            .order_by(
                OpportunityHistoryEvent.occurred_at.desc(),
                OpportunityHistoryEvent.id.desc(),
            )
            .all()
        )
    )
    history_unread_count = unread_history_count(
        db,
        organization_id=_user_org_id(user),
        opportunity_id=opportunity.id,
        user_id=user.id,
    )

    return templates.TemplateResponse("detail.html", {
        "request": request,
        "user": user,
        "opportunity": opportunity,
        "user_opp": user_opp,
        "decision_state": opportunity.decision_state,
        "display_status": _current_org_status(opportunity),
        "normalized_opportunity_type": _normalized_opportunity_type(opportunity),
        "days_until_due": days_until_due,
        "pursue_count": c["pursue"],
        "pass_count": c["pass"],
        "user_vote": user_vote,
        "team_interest_label": _team_interest_label(
            total=c["pursue"],
            current_user_interested=current_user_interested,
        ),
        "teammate_interest_users": teammate_interest_users,
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
        "grants_gov_metadata": grants_gov_metadata,
        "grants_gov_documents": grants_gov_documents,
        "opportunity_notes": notes,
        "pursuit_lanes": pursuit_lanes,
        "history_events": history_events,
        "history_unread_count": history_unread_count,
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

    saved_items = [
        item
        for item in saved_items
        if _normalized_opportunity_type(item.opportunity) == "RFP"
    ]

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
    """Sidebar: My Interested Due Soon + Following."""
    today = date.today()

    # My Interested Due Soon: current user's interest signal.
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
        .filter(Opportunity.decision_state != "ARCHIVED")
        .filter(Opportunity.organization_id == _user_org_id(user))
        .filter(Opportunity.qualification_status == QUALIFICATION_QUALIFIED)
        .order_by(Vote.updated_at.desc(), Vote.id.desc(), Opportunity.response_deadline.asc())
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
        .filter(Opportunity.qualification_status == QUALIFICATION_QUALIFIED)
        .order_by(Opportunity.response_deadline.asc())
        .limit(8)
        .all()
    )

    for opp in my_shortlisted:
        opp.days_until_due = (opp.response_deadline - today).days

    for opp in following:
        opp.days_until_due = (opp.response_deadline - today).days

    return {"my_shortlisted": my_shortlisted, "following": following}
