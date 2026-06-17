from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
import requests
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..grants_gov_client import GrantsGovApiError
from ..ingest_grants_gov import ingest_grants_gov
from ..tenancy import current_org_id

router = APIRouter(prefix="/grants", tags=["grants"])


def _user_org_id(user) -> int:
    return getattr(user, "current_organization_id", None) or user.organization_id


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
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "organization_id": org_id,
                "message": str(exc),
                "received": 0,
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 1,
            },
        )
    except (requests.ConnectionError, requests.Timeout) as exc:
        db.rollback()
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "organization_id": org_id,
                "message": f"Could not reach Grants.gov API. Check network/DNS access from the BidLens server. Detail: {exc}",
                "received": 0,
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 1,
            },
        )
    except GrantsGovApiError as exc:
        db.rollback()
        status_code = 400 if exc.status_code and 400 <= exc.status_code < 500 else 502
        return JSONResponse(
            status_code=status_code,
            content={
                "status": "error",
                "organization_id": org_id,
                "message": str(exc),
                "received": 0,
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 1,
                "grants_gov_status_code": exc.status_code,
            },
        )
    except Exception as exc:
        db.rollback()
        return JSONResponse(
            status_code=502,
            content={
                "status": "error",
                "organization_id": org_id,
                "message": f"Grants.gov pull failed: {exc}",
                "received": 0,
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 1,
            },
        )
    return JSONResponse(status_code=200, content=result)
