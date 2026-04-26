from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional
from ..database import get_db
from ..auth import get_current_user
from ..state_machine import OppState
from ..services import transition_state, cast_vote
from ..models import Opportunity, OpportunityBrief
from sqlalchemy import or_
from datetime import datetime
import os
import requests
from ..services import get_vote_counts


router = APIRouter(prefix="/api", tags=["api"])

class TransitionIn(BaseModel):
    opp_id: int
    to_state: str
    ui_version: str = "v1"
    archive_reason: Optional[str] = None


def require_user(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not getattr(user, "organization", None) or not user.organization.is_active:
        raise HTTPException(status_code=403, detail="Organization inactive")
    return user

def require_user_or_automation(request: Request, db: Session):
    expected = os.getenv("AUTOMATION_API_KEY")
    x_api_key = request.headers.get("x-api-key") or request.headers.get("X-Api-Key")

    if expected and x_api_key and x_api_key == expected:
        return {"automation": True}

    return require_user(request, db)



@router.post("/transition")
def api_transition(payload: TransitionIn, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)

    try:
        to_state = OppState(payload.to_state)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid to_state")

    try:
        new_state = transition_state(
            db,
            org_id=user.organization_id,
            user_id=user.id,
            opp_id=payload.opp_id,
            to_state=to_state,
            ui_version=payload.ui_version,
            archive_reason=payload.archive_reason,
        )
        return {"ok": True, "opp_id": payload.opp_id, "state": new_state.value}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class VoteIn(BaseModel):
    opp_id: int
    vote: str  # "PURSUE" or "PASS"
    ui_version: str = "v1"


def _serialize_sidebar(sidebar: dict) -> dict:
    def _serialize_items(items):
        out = []
        for opp in items:
            out.append({
                "id": opp.id,
                "title": opp.title,
                "days_until_due": getattr(opp, "days_until_due", None),
            })
        return out

    return {
        "my_shortlisted": _serialize_items(sidebar.get("my_shortlisted", [])),
        "following": _serialize_items(sidebar.get("following", [])),
    }


@router.post("/vote")
def api_vote(payload: VoteIn, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)

    if payload.vote not in ("PURSUE", "PASS"):
        raise HTTPException(status_code=400, detail="vote must be PURSUE or PASS")

    try:
        result = cast_vote(
            db,
            org_id=user.organization_id,
            user_id=user.id,
            opp_id=payload.opp_id,
            vote=payload.vote,
            ui_version=payload.ui_version,
        )
        counts = get_vote_counts(db, [payload.opp_id]).get(payload.opp_id, {"pursue": 0, "pass": 0})
        from .opportunities import get_sidebar

        sidebar = get_sidebar(db, user)

        return {
            "ok": True,
            **result,
            "opp_id": payload.opp_id,
            "pursue_count": counts["pursue"],
            "pass_count": counts["pass"],
            "in_my_shortlist": result["state"] == "SHORTLISTED" and result["vote"] == "PURSUE",
            "sidebar": _serialize_sidebar(sidebar),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


REVIEW_STAGES = ["Team Review", "Director Review", "Approved"]

# Allowed stage transitions: current_stage -> set of valid next stages
STAGE_TRANSITIONS = {
    "Team Review":     {"Director Review"},
    "Director Review": {"Approved", "Team Review"},  # can approve or return
    "Approved":        set(),                         # terminal
}


class StageIn(BaseModel):
    opp_id: int
    stage: str


@router.post("/stage")
def api_set_stage(payload: StageIn, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)

    if payload.stage not in REVIEW_STAGES:
        raise HTTPException(status_code=400, detail=f"Invalid stage. Must be one of: {REVIEW_STAGES}")

    opp = db.query(Opportunity).filter(Opportunity.id == payload.opp_id).first()
    if not opp:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    if opp.decision_state != "SHORTLISTED":
        raise HTTPException(status_code=400, detail="Stage only applies to shortlisted opportunities")

    current = opp.review_stage or "Team Review"
    allowed = STAGE_TRANSITIONS.get(current, set())
    if payload.stage not in allowed:
        raise HTTPException(status_code=400, detail=f"Cannot move from '{current}' to '{payload.stage}'")

    opp.review_stage = payload.stage
    opp.stage_changed_at = datetime.utcnow()
    opp.stage_changed_by = user.id
    db.commit()

    return {"ok": True, "opp_id": payload.opp_id, "stage": payload.stage}


@router.get("/opps/pending_enrichment")
def pending_enrichment(request: Request, limit: int = 50, db: Session = Depends(get_db)):
    caller = require_user_or_automation(request, db)
    if isinstance(caller, dict) and caller.get("automation") is True:
        pass
    else:
        raise HTTPException(status_code=401, detail="Automation only endpoint")

    # Pending = no brief row OR brief.status == "pending" OR brief.status == "failed"
    q = (
        db.query(Opportunity)
        .outerjoin(OpportunityBrief, OpportunityBrief.opportunity_id == Opportunity.id)
        .filter(
            or_(
                OpportunityBrief.id.is_(None),
                OpportunityBrief.status.in_(["pending", "failed"])
            )
        )
        .order_by(Opportunity.response_deadline.asc())
        .limit(limit)
    )

    opps = q.all()

    out = []
    for o in opps:
        # give n8n the text it needs
        text_for_enrichment = (o.description or "").strip()
        out.append({
            "id": o.id,
            "title": o.title,
            "agency": o.agency,
            "opportunity_type": o.opportunity_type,
            "posted_date": o.posted_date.isoformat() if o.posted_date else None,
            "response_deadline": o.response_deadline.isoformat() if o.response_deadline else None,
            "naics": o.naics,
            "set_aside": o.set_aside,
            "url": o.sam_url,
            "text_for_enrichment": text_for_enrichment[:20000],  # guardrail
        })
    return out
    
class BriefIn(BaseModel):
    brief: dict
    model: Optional[str] = None

@router.post("/opps/{opp_id}/enrichment")
def save_enrichment(opp_id: int, payload: BriefIn, request: Request, db: Session = Depends(get_db)):
    caller = require_user_or_automation(request, db)
    opp = db.query(Opportunity).filter(Opportunity.id == opp_id).first()
    if not opp:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    row = db.query(OpportunityBrief).filter(OpportunityBrief.opportunity_id == opp_id).first()
    if not row:
        row = OpportunityBrief(opportunity_id=opp_id)
        db.add(row)
        db.flush()

    row.brief_json = payload.brief
    row.model = payload.model
    row.generated_at = datetime.utcnow()
    row.status = "ok"
    row.error_message = None

    db.commit()
    return {"ok": True, "opp_id": opp_id}
    
@router.post("/opps/{opp_id}/enrichment/reset")
def reset_enrichment(opp_id: int, request: Request, db: Session = Depends(get_db)):
    caller = require_user_or_automation(request, db)

    row = db.query(OpportunityBrief).filter(OpportunityBrief.opportunity_id == opp_id).first()
    if not row:
        row = OpportunityBrief(opportunity_id=opp_id)
        db.add(row)
        db.flush()

    row.status = "pending"
    row.error_message = None
    row.brief_json = None
    row.model = None
    row.generated_at = None

    db.commit()
    return {"ok": True, "opp_id": opp_id, "status": "pending"}
    
@router.post("/opps/{opp_id}/mark_pending")
def mark_pending(opp_id: int, request: Request, db: Session = Depends(get_db)):
    caller = require_user_or_automation(request, db)

    row = db.query(OpportunityBrief).filter(OpportunityBrief.opportunity_id == opp_id).first()
    if not row:
        row = OpportunityBrief(opportunity_id=opp_id)
        db.add(row)
        db.flush()

    row.status = "pending"
    row.error_message = None
    db.commit()
    return {"ok": True, "opp_id": opp_id, "status": "pending"}

@router.post("/opps/{opp_id}/generate_brief")
def generate_brief(
    opp_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_user(request, db)

    opp = db.query(Opportunity).filter(Opportunity.id == opp_id).first()
    if not opp:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    row = db.query(OpportunityBrief).filter(
        OpportunityBrief.opportunity_id == opp_id
    ).first()

    if not row:
        row = OpportunityBrief(
            opportunity_id=opp_id,
            status="pending",
        )
        db.add(row)
    else:
        row.status = "pending"
        row.error_message = None

    db.commit()

    # Call n8n webhook
    N8N_WEBHOOK_URL = os.getenv("N8N_BRIEF_WEBHOOK_URL")
    print("N8N_WEBHOOK =", N8N_WEBHOOK_URL)
    requests.post(
        N8N_WEBHOOK_URL,
        json={"opp_id": opp_id},
        timeout=5,
    )

    return {"ok": True, "status": "pending"}
    
@router.get("/opps/{opp_id}")
def get_opp_for_enrichment(opp_id: int, request: Request, db: Session = Depends(get_db)):
    caller = require_user_or_automation(request, db)
    o = db.query(Opportunity).filter(Opportunity.id == opp_id).first()
    if not o:
        raise HTTPException(status_code=404, detail="Not found")

    return {
        "id": o.id,
        "title": o.title,
        "agency": o.agency,
        "opportunity_type": o.opportunity_type,
        "posted_date": o.posted_date.isoformat() if o.posted_date else None,
        "response_deadline": o.response_deadline.isoformat() if o.response_deadline else None,
        "naics": o.naics,
        "set_aside": o.set_aside,
        "url": o.sam_url,
        "text_for_enrichment": (o.description or "").strip()[:20000],
    }
