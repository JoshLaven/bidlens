from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
import requests
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..grants_gov_client import GrantsGovApiError
from ..ingest_grants_gov import ingest_grants_gov
from ..services.ingestion_runs import record_source_activity
from ..tenancy import current_org_id

router = APIRouter(prefix="/grants", tags=["grants"])


def _user_org_id(user) -> int:
    return getattr(user, "current_organization_id", None) or user.organization_id


def _record_grants_source_activity(
    db: Session,
    *,
    org_id: int,
    user_id: int,
    result: dict,
    reason_code: str | None = None,
) -> None:
    reason_counts = {reason_code: 1} if reason_code else None
    reason_labels = {reason_code: result.get("message", reason_code)} if reason_code else None
    record_source_activity(
        db,
        source="grants.gov",
        organization_id=org_id,
        user_id=user_id,
        filename="Manual Grants.gov pull",
        result=result,
        processed_count=int(result.get("received", 0) or 0),
        created_count=int(result.get("created", 0) or 0),
        updated_count=int(result.get("updated", 0) or 0),
        unchanged_count=0,
        skipped_count=int(result.get("skipped", 0) or 0),
        error_count=int(result.get("errors", 0) or 0),
        reason_counts=reason_counts,
        reason_labels=reason_labels,
        notes=result.get("message"),
    )
    db.commit()


@router.post("/pull-now", response_model=None)
def pull_now(
    request: Request,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user:
        return JSONResponse(status_code=401, content={"status": "error", "message": "Login required."})

    setattr(user, "current_organization_id", current_org_id(request, db, user))
    org_id = _user_org_id(user)
    try:
        result = ingest_grants_gov(db, organization_id=org_id)
    except RuntimeError as exc:
        db.rollback()
        result = {
            "status": "error",
            "organization_id": org_id,
            "message": str(exc),
            "received": 0,
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "errors": 1,
        }
        _record_grants_source_activity(db, org_id=org_id, user_id=user.id, result=result, reason_code="runtime_error")
        return JSONResponse(status_code=400, content=result)
    except (requests.ConnectionError, requests.Timeout) as exc:
        db.rollback()
        result = {
            "status": "error",
            "organization_id": org_id,
            "message": f"Could not reach Grants.gov API. Check network/DNS access from the BidLens server. Detail: {exc}",
            "received": 0,
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "errors": 1,
        }
        _record_grants_source_activity(db, org_id=org_id, user_id=user.id, result=result, reason_code="connection_error")
        return JSONResponse(status_code=503, content=result)
    except GrantsGovApiError as exc:
        db.rollback()
        status_code = 400 if exc.status_code and 400 <= exc.status_code < 500 else 502
        result = {
            "status": "error",
            "organization_id": org_id,
            "message": str(exc),
            "received": 0,
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "errors": 1,
            "grants_gov_status_code": exc.status_code,
        }
        _record_grants_source_activity(db, org_id=org_id, user_id=user.id, result=result, reason_code="grants_gov_api_error")
        return JSONResponse(status_code=status_code, content=result)
    except Exception as exc:
        db.rollback()
        result = {
            "status": "error",
            "organization_id": org_id,
            "message": f"Grants.gov pull failed: {exc}",
            "received": 0,
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "errors": 1,
        }
        _record_grants_source_activity(db, org_id=org_id, user_id=user.id, result=result, reason_code="import_error")
        return JSONResponse(status_code=502, content=result)
    _record_grants_source_activity(db, org_id=org_id, user_id=user.id, result=result)
    return JSONResponse(status_code=200, content=result)
