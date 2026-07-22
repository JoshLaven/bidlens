# src/bidlens/routes/sam.py
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..database import get_db
from ..ingest_sam import (
    backfill_opportunity_descriptions,
    sam_ingest_in_progress,
)
from ..models import OrganizationMembership
from ..auth import get_current_user  # <-- adjust this import to your project
from ..services.sam_pulls import (
    execute_sam_source_pull,
    failed_naics,
    find_sam_source_config,
    record_sam_noop_activity,
    record_sam_source_activity,
    retry_after_display,
    retry_after_header_value,
    sam_busy_payload,
)
from ..tenancy import current_org_id

router = APIRouter(prefix="/sam", tags=["sam"])


def _user_org_id(user) -> int:
    return getattr(user, "current_organization_id", None) or user.organization_id


def require_org_admin(user, db: Session):
    membership = (
        db.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == _user_org_id(user),
            OrganizationMembership.user_id == user.id,
        )
        .first()
    )
    if not membership or membership.role != "admin":
        raise HTTPException(status_code=403, detail="Only the org admin can run this action.")
    return user


class DescriptionBackfillIn(BaseModel):
    limit: int = 25


@router.post("/pull-now", response_model=None)
def pull_now(
    request: Request,
    search_id: int | None = None,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user:
        return JSONResponse(status_code=401, content={"status": "error", "message": "Login required."})
    setattr(user, "current_organization_id", current_org_id(request, db, user))
    org_id = _user_org_id(user)
    require_org_admin(user, db)
    if sam_ingest_in_progress():
        return JSONResponse(status_code=409, content=sam_busy_payload(organization_id=org_id))

    config = find_sam_source_config(db, organization_id=org_id, search_id=search_id)
    if not config:
        result = record_sam_noop_activity(
            db,
            organization_id=org_id,
            user_id=user.id,
            reason="missing_sam_source_config",
            message=(
                "The selected SAM.gov saved search was not found."
                if search_id is not None
                else "Configure a SAM.gov saved search before running a pull."
            ),
        )
        return JSONResponse(status_code=400, content=result)

    try:
        result = execute_sam_source_pull(
            db,
            organization_id=org_id,
            config=config,
            run_type="Manual",
            manual_pull=True,
            enrich_descriptions=False,
        )
    except RuntimeError as exc:
        if str(exc) == "A SAM pull is already in progress":
            return JSONResponse(status_code=409, content=sam_busy_payload(organization_id=org_id))
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

    retry_after_display_value = retry_after_display(retry_after, retry_after_seconds)
    retry_after_header = retry_after_header_value(retry_after, retry_after_seconds)
    result["retry_after"] = retry_after_display_value
    result["retry_after_seconds"] = retry_after_seconds
    result["failed_naics"] = failed_naics(rate_limited_results or sam_unavailable_results)
    result["organization_id"] = org_id

    if result.get("status") == "paused_rate_limit":
        retry_hint = f" Retry after: {retry_after_display_value}." if retry_after_display_value else ""
        result["message"] = f"Paused — SAM quota exceeded.{retry_hint}"
        headers = {"Retry-After": retry_after_header} if retry_after_header else {}
        record_sam_source_activity(db, organization_id=org_id, user_id=user.id, result=result)
        return JSONResponse(status_code=200, content=result, headers=headers)
    if sam_unavailable_results:
        if result.get("status") == "failed":
            result["message"] = "SAM.gov is temporarily unavailable. Try again later."
        else:
            result["message"] = (
                f"Pull partially completed: {result['inserted']} inserted, {result['updated']} updated, "
                f"{result['skipped']} skipped, {result['filtered']} filtered, {result['errors']} errors. "
                "SAM.gov is temporarily unavailable. Try again later."
            )
        record_sam_source_activity(db, organization_id=org_id, user_id=user.id, result=result)
        return JSONResponse(status_code=503 if result.get("status") == "failed" else 200, content=result)
    elif all_rate_limited:
        retry_hint = f" Try again after {retry_after_display_value}." if retry_after_display_value else " Try again later."
        result["status"] = "rate_limited"
        result["message"] = f"SAM.gov quota exceeded.{retry_hint}"
        headers = {"Retry-After": retry_after_header} if retry_after_header else {}
        record_sam_source_activity(db, organization_id=org_id, user_id=user.id, result=result)
        return JSONResponse(status_code=429, content=result, headers=headers)
    elif rate_limited_results:
        wait_hint = f" Try again after {retry_after_display_value}." if retry_after_display_value else ""
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
        record_sam_source_activity(db, organization_id=org_id, user_id=user.id, result=result)
        return JSONResponse(status_code=200, content=result)
    else:
        result["message"] = (
            f"Pull completed with {result['inserted']} inserted, {result['updated']} updated, "
            f"{result['skipped']} skipped, {result['filtered']} filtered, {result['errors']} errors, "
            f"{result.get('pages_pulled', 0)} pages pulled, {result.get('records_seen', 0)} records seen."
        )
        record_sam_source_activity(db, organization_id=org_id, user_id=user.id, result=result)
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
