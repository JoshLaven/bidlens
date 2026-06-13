# src/bidlens/routes/sam.py
import datetime as dt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
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
from ..tenancy import current_org_id

router = APIRouter(prefix="/sam", tags=["sam"])


def _user_org_id(user) -> int:
    return getattr(user, "current_organization_id", None) or user.organization_id


def require_org_admin(user, db: Session):
    first_user = (
        db.query(User)
        .filter(User.organization_id == _user_org_id(user))
        .order_by(User.id.asc())
        .first()
    )
    if not first_user or first_user.id != user.id:
        raise HTTPException(status_code=403, detail="Only the org admin can run this action.")
    return user


class DescriptionBackfillIn(BaseModel):
    limit: int = 25


def _retry_after_display(retry_after: str | None, retry_after_seconds: float | None) -> str | None:
    if retry_after:
        return retry_after
    if retry_after_seconds is None:
        return None

    retry_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=retry_after_seconds)
    return retry_at.strftime("%a, %d %b %Y %H:%M:%S GMT")


def _failed_naics(results: list[dict]) -> list[str]:
    return [item.get("naics") for item in results if item.get("naics")]


def _retry_after_header_value(retry_after: str | None, retry_after_seconds: float | None) -> str | None:
    if retry_after:
        return retry_after
    if retry_after_seconds is None:
        return None
    return str(int(round(retry_after_seconds)))

@router.post("/pull-now", response_model=None)
def pull_now(
    request: Request,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    setattr(user, "current_organization_id", current_org_id(request, db, user))
    org_id = _user_org_id(user)
    if sam_ingest_in_progress():
        return JSONResponse(status_code=409, content={
            "status": "busy",
            "organization_id": org_id,
            "message": "A SAM pull is already in progress. Wait for it to finish before starting another.",
            "run_id": None,
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
            "filtered": 0,
            "errors": 0,
            "results": [],
        })

    profile = db.query(OrgProfile).filter(OrgProfile.org_id == org_id).first()
    if not profile:
        profile = OrgProfile(org_id=org_id, sam_naics_codes="541611,541690")
        db.add(profile)
        db.commit()
        db.refresh(profile)

    naics_list = [x.strip() for x in (profile.sam_naics_codes or "").split(",") if x.strip()]
    days_back = profile.sam_days_back or 7
    allowed_types = parse_allowed_types(profile.sam_allowed_types)
    if not naics_list:
        return JSONResponse(status_code=400, content={
            "status": "noop",
            "organization_id": org_id,
            "message": "No NAICS codes are configured for this organization.",
            "run_id": None,
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
            "filtered": 0,
            "errors": 0,
            "results": [],
        })

    try:
        result = ingest_sam(
            db,
            organization_id=org_id,
            naics_list=naics_list,
            days_back=days_back,
            allowed_types=allowed_types,
            manual_pull=True,
            enrich_descriptions=False,
        )
    except RuntimeError as exc:
        if str(exc) == "A SAM pull is already in progress":
            return JSONResponse(status_code=409, content={
                "status": "busy",
                "organization_id": org_id,
                "message": "A SAM pull is already in progress. Wait for it to finish before starting another.",
                "run_id": None,
                "inserted": 0,
                "updated": 0,
                "skipped": 0,
                "filtered": 0,
                "errors": 0,
                "results": [],
            })
        raise

    rate_limited_results = [item for item in result.get("results", []) if item.get("error_type") == "rate_limited"]
    sam_unavailable_results = [item for item in result.get("results", []) if item.get("error_type") == "sam_unavailable"]
    stopped_due_to_rate_limit = bool(result.get("stopped_due_to_rate_limit"))
    all_rate_limited = (
        bool(rate_limited_results)
        and result.get("status") == "rate_limited"
        and result.get("inserted", 0) == 0
        and result.get("updated", 0) == 0
        and result.get("skipped", 0) == 0
        and result.get("filtered", 0) == 0
    )

    retry_after_seconds = result.get("retry_after_seconds")
    retry_after = result.get("retry_after")
    for item in rate_limited_results:
        seconds = item.get("retry_after_seconds")
        if seconds is not None and (retry_after_seconds is None or seconds > retry_after_seconds):
            retry_after_seconds = seconds
            retry_after = item.get("retry_after") or retry_after
        elif retry_after is None and item.get("retry_after"):
            retry_after = item.get("retry_after")

    retry_after_display = _retry_after_display(retry_after, retry_after_seconds)
    retry_after_header = _retry_after_header_value(retry_after, retry_after_seconds)
    result["retry_after"] = retry_after_display
    result["retry_after_seconds"] = retry_after_seconds
    result["failed_naics"] = _failed_naics(rate_limited_results or sam_unavailable_results)
    result["organization_id"] = org_id

    if sam_unavailable_results:
        if result.get("status") == "failed":
            result["message"] = "SAM.gov is temporarily unavailable. Try again later."
        else:
            result["message"] = (
                f"Pull partially completed: {result['inserted']} inserted, {result['updated']} updated, "
                f"{result['skipped']} skipped, {result['filtered']} filtered, {result['errors']} errors. "
                "SAM.gov is temporarily unavailable. Try again later."
            )
        return JSONResponse(status_code=503 if result.get("status") == "failed" else 200, content=result)
    elif all_rate_limited:
        retry_hint = f" Try again after {retry_after_display}." if retry_after_display else " Try again later."
        result["status"] = "rate_limited"
        result["message"] = f"SAM.gov quota exceeded.{retry_hint}"
        headers = {"Retry-After": retry_after_header} if retry_after_header else {}
        return JSONResponse(status_code=429, content=result, headers=headers)
    elif rate_limited_results:
        wait_hint = f" Try again after {retry_after_display}." if retry_after_display else ""
        if stopped_due_to_rate_limit:
            result["message"] = (
                f"Pull stopped after SAM.gov quota exceeded: {result['inserted']} inserted, {result['updated']} updated, "
                f"{result['skipped']} skipped, {result['filtered']} filtered, {result['errors']} errors.{wait_hint}"
            )
        else:
            result["message"] = (
                f"Pull partially completed: {result['inserted']} inserted, {result['updated']} updated, "
                f"{result['skipped']} skipped, {result['filtered']} filtered, {result['errors']} errors."
                f" SAM.gov rate limited one or more NAICS pulls.{wait_hint}"
            )
        return JSONResponse(status_code=200, content=result)
    else:
        result["message"] = (
            f"Pull completed with {result['inserted']} inserted, {result['updated']} updated, "
            f"{result['skipped']} skipped, {result['filtered']} filtered, {result['errors']} errors, "
            f"{result.get('pages_pulled', 0)} pages pulled, {result.get('records_seen', 0)} records seen."
        )
        return JSONResponse(status_code=200, content=result)


@router.post("/backfill-descriptions", response_model=None)
def backfill_descriptions(
    payload: DescriptionBackfillIn,
    request: Request,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    setattr(user, "current_organization_id", current_org_id(request, db, user))
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
