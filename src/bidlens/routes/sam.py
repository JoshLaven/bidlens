# src/bidlens/routes/sam.py
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..database import get_db
from ..ingest_sam import (
    backfill_opportunity_descriptions,
    ingest_sam,
    parse_allowed_types,
    sam_ingest_in_progress,
)
from ..models import OrgProfile, User
from ..auth import get_current_user  # <-- adjust this import to your project

router = APIRouter(prefix="/sam", tags=["sam"])


def require_org_admin(user, db: Session):
    first_user = (
        db.query(User)
        .filter(User.organization_id == user.organization_id)
        .order_by(User.id.asc())
        .first()
    )
    if not first_user or first_user.id != user.id:
        raise HTTPException(status_code=403, detail="Only the org admin can run this action.")
    return user


class DescriptionBackfillIn(BaseModel):
    limit: int = 25

@router.post("/pull-now", response_model=None)
def pull_now(
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if sam_ingest_in_progress():
        return {
            "status": "busy",
            "message": "A SAM pull is already in progress. Wait for it to finish before starting another.",
            "run_id": None,
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
            "filtered": 0,
            "errors": 0,
            "results": [],
        }

    profile = db.query(OrgProfile).filter(OrgProfile.org_id == user.organization_id).first()
    if not profile:
        profile = OrgProfile(org_id=user.organization_id, sam_naics_codes="541611,541690")
        db.add(profile)
        db.commit()
        db.refresh(profile)

    naics_list = [x.strip() for x in (profile.sam_naics_codes or "").split(",") if x.strip()]
    days_back = profile.sam_days_back or 7
    allowed_types = parse_allowed_types(profile.sam_allowed_types)
    if not naics_list:
        return {
            "status": "noop",
            "message": "No NAICS codes are configured for this organization.",
            "run_id": None,
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
            "filtered": 0,
            "errors": 0,
            "results": [],
        }

    try:
        result = ingest_sam(db, naics_list=naics_list, days_back=days_back, allowed_types=allowed_types)
    except RuntimeError as exc:
        if str(exc) == "A SAM pull is already in progress":
            return {
                "status": "busy",
                "message": "A SAM pull is already in progress. Wait for it to finish before starting another.",
                "run_id": None,
                "inserted": 0,
                "updated": 0,
                "skipped": 0,
                "filtered": 0,
                "errors": 0,
                "results": [],
            }
        raise

    rate_limited_results = [item for item in result.get("results", []) if item.get("error_type") == "rate_limited"]
    sam_unavailable_results = [item for item in result.get("results", []) if item.get("error_type") == "sam_unavailable"]
    if sam_unavailable_results:
        if result.get("status") == "failed":
            result["message"] = "SAM.gov is temporarily unavailable. Try again later."
        else:
            result["message"] = (
                f"Pull partially completed: {result['inserted']} inserted, {result['updated']} updated, "
                f"{result['skipped']} skipped, {result['filtered']} filtered, {result['errors']} errors. "
                "SAM.gov is temporarily unavailable. Try again later."
            )
    elif rate_limited_results:
        waits = [item.get("retry_after_seconds") for item in rate_limited_results if item.get("retry_after_seconds") is not None]
        wait_hint = f" Retry after about {int(round(max(waits)))} seconds." if waits else ""
        result["message"] = (
            f"Pull partially completed: {result['inserted']} inserted, {result['updated']} updated, "
            f"{result['skipped']} skipped, {result['filtered']} filtered, {result['errors']} errors."
            f" SAM.gov rate limited one or more NAICS pulls.{wait_hint}"
        )
    else:
        result["message"] = (
            f"Pull completed with {result['inserted']} inserted, {result['updated']} updated, "
            f"{result['skipped']} skipped, {result['filtered']} filtered, {result['errors']} errors."
        )
    return result


@router.post("/backfill-descriptions", response_model=None)
def backfill_descriptions(
    payload: DescriptionBackfillIn,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_org_admin(user, db)

    limit = max(1, min(payload.limit, 100))
    result = backfill_opportunity_descriptions(db, limit=limit)

    if result["rate_limited"]:
        waits = [
            item.get("retry_after_seconds")
            for item in result["results"]
            if item.get("status") == "rate_limited" and item.get("retry_after_seconds") is not None
        ]
        wait_hint = f" Retry after about {int(round(max(waits)))} seconds." if waits else ""
        result["status"] = "partial_success"
        result["message"] = (
            f"Description backfill partially completed: {result['updated']} updated, "
            f"{result['skipped']} skipped, {result['errors']} errors.{wait_hint}"
        )
    else:
        result["status"] = "success"
        result["message"] = (
            f"Description backfill completed: {result['updated']} updated, "
            f"{result['skipped']} skipped, {result['errors']} errors."
        )

    return result
