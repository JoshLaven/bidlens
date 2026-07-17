from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session
from urllib.parse import parse_qsl, urlencode

from ..auth import attach_request_user_context, get_current_user
from ..database import get_db
from ..models import OrganizationMembership, OpportunityPursuitLaneMatch, PursuitLane, User
from ..services.pursuit_lanes import (
    parse_list,
    refresh_lane_matches,
    refresh_org_lane_matches,
    set_user_my_lanes,
    user_my_lanes,
)
from .opportunities import get_sidebar

router = APIRouter()
templates = Jinja2Templates(directory="src/bidlens/templates")


def require_user(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None
    attach_request_user_context(request, db, user)
    return user


def _user_org_id(user) -> int:
    return getattr(user, "current_organization_id", None) or user.organization_id


def _can_manage_lanes(db: Session, user: User) -> bool:
    """Temporary v1 lane management gate.

    Until BidLens has full auth/workspace roles in the UI, any member of the
    current workspace may manage pursuit lanes. Organization scoping still
    prevents cross-workspace reads and writes.
    """
    membership = (
        db.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == _user_org_id(user),
            OrganizationMembership.user_id == user.id,
        )
        .first()
    )
    return bool(membership)


def _redirect(
    request: Request,
    db: Session | None = None,
    user: User | None = None,
    *,
    saved: bool = False,
) -> RedirectResponse:
    params = [
        (key, value)
        for key, value in parse_qsl(str(request.url.query or ""), keep_blank_values=False)
        if key != "saved"
    ]
    if saved:
        params.append(("saved", "1"))
    query = urlencode(params)
    suffix = f"?{query}" if query else ""
    return RedirectResponse(url=f"/settings{suffix}", status_code=303)


@router.get("/pursuit-lanes")
async def pursuit_lanes_page(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    suffix = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(url=f"/settings{suffix}", status_code=303)


def lane_management_context(db: Session, user: User) -> dict:
    org_id = _user_org_id(user)
    lanes = (
        db.query(PursuitLane)
        .filter(PursuitLane.organization_id == org_id)
        .order_by(PursuitLane.is_active.desc(), PursuitLane.name.asc())
        .all()
    )
    match_counts = {
        lane_id: count
        for lane_id, count in (
            db.query(
                OpportunityPursuitLaneMatch.pursuit_lane_id,
                func.count(OpportunityPursuitLaneMatch.id),
            )
            .filter(OpportunityPursuitLaneMatch.organization_id == org_id)
            .group_by(OpportunityPursuitLaneMatch.pursuit_lane_id)
            .all()
        )
    }
    my_lanes = user_my_lanes(db, organization_id=org_id, user_id=user.id)
    my_lane_ids = {lane.id for lane in my_lanes}

    return {
        "lanes": lanes,
        "match_counts": match_counts,
        "my_lanes": my_lanes,
        "my_lane_ids": my_lane_ids,
        "can_manage_lanes": _can_manage_lanes(db, user),
    }


@router.post("/pursuit-lanes")
async def create_pursuit_lane(
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
    agencies: str = Form(""),
    naics: str = Form(""),
    keywords: str = Form(""),
    set_asides: str = Form(""),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not _can_manage_lanes(db, user):
        return _redirect(request, db, user)

    lane_name = name.strip()
    if not lane_name:
        return _redirect(request)

    org_id = _user_org_id(user)
    lane = PursuitLane(
        organization_id=org_id,
        name=lane_name,
        description=description.strip() or None,
        agencies=parse_list(agencies),
        naics=parse_list(naics),
        keywords=parse_list(keywords),
        set_asides=parse_list(set_asides),
        is_active=bool(is_active),
    )
    db.add(lane)
    db.flush()
    refresh_lane_matches(db, org_id, lane)
    db.commit()
    return _redirect(request, db, user, saved=True)


@router.post("/pursuit-lanes/{lane_id}")
async def update_pursuit_lane(
    request: Request,
    lane_id: int,
    name: str = Form(""),
    description: str = Form(""),
    agencies: str = Form(""),
    naics: str = Form(""),
    keywords: str = Form(""),
    set_asides: str = Form(""),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not _can_manage_lanes(db, user):
        return _redirect(request, db, user)

    org_id = _user_org_id(user)
    lane = (
        db.query(PursuitLane)
        .filter(PursuitLane.id == lane_id, PursuitLane.organization_id == org_id)
        .first()
    )
    if not lane:
        return _redirect(request, db, user)

    lane_name = name.strip()
    if lane_name:
        lane.name = lane_name
    lane.description = description.strip() or None
    lane.agencies = parse_list(agencies)
    lane.naics = parse_list(naics)
    lane.keywords = parse_list(keywords)
    lane.set_asides = parse_list(set_asides)
    lane.is_active = bool(is_active)
    refresh_lane_matches(db, org_id, lane)
    db.commit()
    return _redirect(request, db, user, saved=True)


@router.post("/pursuit-lanes/my-lanes")
async def update_my_lanes(
    request: Request,
    lane_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not _can_manage_lanes(db, user):
        return _redirect(request, db, user)

    set_user_my_lanes(
        db,
        organization_id=_user_org_id(user),
        user_id=user.id,
        lane_ids=lane_ids,
    )
    db.commit()
    return _redirect(request, db, user, saved=True)


@router.post("/pursuit-lanes/rematch")
async def rematch_pursuit_lanes(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if _can_manage_lanes(db, user):
        refresh_org_lane_matches(db, _user_org_id(user))
        db.commit()
    return _redirect(request, db, user, saved=True)
