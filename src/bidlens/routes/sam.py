# src/bidlens/routes/sam.py
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from ..database import get_db
from ..ingest_sam import ingest_sam, parse_allowed_types
from ..models import OrgProfile
from ..auth import get_current_user  # <-- adjust this import to your project

router = APIRouter(prefix="/sam", tags=["sam"])

@router.post("/pull-now", response_model=None)
def pull_now(
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = db.query(OrgProfile).filter(OrgProfile.org_id == user.organization_id).one()
    print("USER org:", user.organization_id, "PROFILE org:", profile.org_id)

    naics_list = [x.strip() for x in (profile.sam_naics_codes or "").split(",") if x.strip()]
    days_back = profile.sam_days_back or 7
    allowed_types = parse_allowed_types(profile.sam_allowed_types)
    print("NAICS LIST:", naics_list, "raw:", repr(profile.sam_naics_codes))
    return ingest_sam(db, naics_list=naics_list, days_back=days_back, allowed_types=allowed_types)