# src/bidlens/routers/sam.py
import os
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..ingest_sam import ingest_sam

router = APIRouter(prefix="/sam", tags=["sam"])

@router.post("/pull-now")
def pull_now(db: Session = Depends(get_db)):
    naics_env = os.getenv("SAM_NAICS", "541611,541690")
    naics_list = [x.strip() for x in naics_env.split(",") if x.strip()]
    return ingest_sam(db, naics_list=naics_list, days_back=7)
