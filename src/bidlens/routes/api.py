from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from ..database import get_db
from ..auth import get_current_user
from ..state_machine import OppState
from ..services import transition_state, set_vote

router = APIRouter(prefix="/api", tags=["api"])

class TransitionIn(BaseModel):
    opp_id: int
    to_state: str
    ui_version: str = "v1"

class VoteIn(BaseModel):
    opp_id: int
    vote: Optional[str] = None  # "UP", "DOWN", or null
    ui_version: str

def require_user(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not getattr(user, "organization", None) or not user.organization.is_active:
        raise HTTPException(status_code=403, detail="Organization inactive")
    return user

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
        )
        return {"ok": True, "opp_id": payload.opp_id, "state": new_state.value}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/vote")
def api_vote(payload: VoteIn, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)

    v = payload.vote
    if isinstance(v, str):
        v = v.upper()
    if v not in ("UP", "DOWN", None):
        raise HTTPException(status_code=400, detail="vote must be UP, DOWN, or null")

    set_vote(
        db,
        org_id=user.organization_id,
        user_id=user.id,
        opp_id=payload.opp_id,
        vote=v,
        ui_version=payload.ui_version,
    )
    return {"ok": True, "opp_id": payload.opp_id, "vote": v}
