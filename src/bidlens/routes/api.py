from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Any, Optional
from ..database import get_db
from ..auth import get_current_user
from ..state_machine import OppState
from ..services import transition_state, cast_vote
from ..models import Opportunity, OpportunityBrief
from sqlalchemy import or_
from datetime import datetime
import logging
from ..services import get_vote_counts, get_vote_user_maps
from ..sam_client import _is_url_like
from ..services.research.brief_generator import (
    build_brief_request_payload,
    generate_local_brief,
    generate_llm_brief,
)
router = APIRouter(prefix="/api", tags=["api"])
logger = logging.getLogger(__name__)


def _best_description_text(opp: Opportunity) -> str:
    description_text = (opp.description_text or "").strip()
    if description_text:
        return description_text

    description = (opp.description or "").strip()
    if description and not _is_url_like(description):
        return description

    return ""


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
            due_date = getattr(opp, "response_deadline", None)
            days_until_due = getattr(opp, "days_until_due", None)
            due_label = None
            if due_date and days_until_due is not None:
                due_label = f"Due {due_date.strftime('%b')} {due_date.day} ({days_until_due}D)"
            out.append({
                "id": opp.id,
                "title": opp.title,
                "days_until_due": getattr(opp, "days_until_due", None),
                "due_label": due_label,
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
        pursue_users_map, pass_users_map = get_vote_user_maps(
            db,
            org_id=user.organization_id,
            opp_ids=[payload.opp_id],
        )
        from .opportunities import get_sidebar

        sidebar = get_sidebar(db, user)

        return {
            "ok": True,
            **result,
            "opp_id": payload.opp_id,
            "pursue_count": counts["pursue"],
            "pass_count": counts["pass"],
            "pursue_users": pursue_users_map.get(payload.opp_id, []),
            "pass_users": pass_users_map.get(payload.opp_id, []),
            "in_my_shortlist": result["state"] == "SHORTLISTED" and result["vote"] == "PURSUE",
            "sidebar": _serialize_sidebar(sidebar),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/opps/{opp_id}/preview")
def opportunity_preview(
    opp_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_user(request, db)

    opp = db.query(Opportunity).filter(Opportunity.id == opp_id).first()
    if not opp:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    description = _best_description_text(opp)

    if description:
        return {
            "ok": True,
            "state": "text",
            "description": description[:400] + ("…" if len(description) > 400 else ""),
            "sam_url": opp.sam_url,
        }

    if opp.sam_url:
        return {
            "ok": True,
            "state": "sam_fallback",
            "description": "Detailed description available on SAM.gov",
            "sam_url": opp.sam_url,
        }

    return {
        "ok": True,
        "state": "empty",
        "description": "No description available.",
        "sam_url": None,
    }


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
        text_for_enrichment = _best_description_text(o)
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
    provider: Optional[str] = None
    model: Optional[str] = None
    source_basis: Optional[str] = None
    sources_used: Optional[list[dict[str, Any]]] = None
    filenames_processed: Optional[list[str]] = None
    source_summary: Optional[dict[str, Any]] = None


def _apply_brief_source_metadata(row: OpportunityBrief, payload: dict[str, Any]) -> None:
    row.provider = payload.get("provider")
    row.source_basis = payload.get("source_basis")
    row.sources_used = payload.get("sources_used")
    row.filenames_processed = payload.get("filenames_processed")
    row.source_summary = payload.get("source_summary")

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
    row.provider = payload.provider
    row.model = payload.model
    row.generated_at = datetime.utcnow()
    row.status = "ok"
    row.error_message = None
    if payload.source_basis or payload.sources_used or payload.filenames_processed or payload.source_summary:
        _apply_brief_source_metadata(
            row,
            {
                "source_basis": payload.source_basis,
                "sources_used": payload.sources_used,
                "filenames_processed": payload.filenames_processed,
                "source_summary": payload.source_summary,
            },
        )

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
    row.provider = None
    row.generated_at = None
    row.source_basis = None
    row.sources_used = None
    row.filenames_processed = None
    row.source_summary = None

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

    brief_payload = build_brief_request_payload(opp)
    _apply_brief_source_metadata(row, brief_payload)
    db.commit()

    fallback_triggered = False
    try:
        llm_result = generate_llm_brief(brief_payload)
        row.brief_json = llm_result["brief"]
        row.provider = llm_result["provider"]
        row.model = llm_result["model"]
        row.generated_at = datetime.utcnow()
        row.status = "ok"
        row.error_message = None
    except Exception as exc:
        fallback_triggered = True
        logger.warning("OpenAI brief generation failed for opp_id=%s; using local fallback error=%s", opp_id, repr(exc))
        row.brief_json = generate_local_brief(opp, brief_payload)
        row.provider = "local"
        row.model = "local-deterministic-fallback"
        row.generated_at = datetime.utcnow()
        row.status = "ok"
        row.error_message = f"OpenAI brief generation failed, used local fallback: {exc}"
        db.commit()
        return {
            "ok": True,
            "status": "ok",
            "source_basis": brief_payload["source_basis"],
            "filenames_processed": brief_payload["filenames_processed"],
            "provider": row.provider,
            "model": row.model,
            "fallback_triggered": fallback_triggered,
        }

    db.commit()

    return {
        "ok": True,
        "status": "ok",
        "source_basis": brief_payload["source_basis"],
        "filenames_processed": brief_payload["filenames_processed"],
        "provider": row.provider,
        "model": row.model,
        "fallback_triggered": fallback_triggered,
    }
    
@router.get("/opps/{opp_id}")
def get_opp_for_enrichment(opp_id: int, request: Request, db: Session = Depends(get_db)):
    caller = require_user_or_automation(request, db)
    o = db.query(Opportunity).filter(Opportunity.id == opp_id).first()
    if not o:
        raise HTTPException(status_code=404, detail="Not found")

    payload = build_brief_request_payload(o)

    row = db.query(OpportunityBrief).filter(OpportunityBrief.opportunity_id == opp_id).first()
    if not row:
        row = OpportunityBrief(opportunity_id=opp_id)
        db.add(row)
        db.flush()
    _apply_brief_source_metadata(row, payload)
    db.commit()

    return payload
