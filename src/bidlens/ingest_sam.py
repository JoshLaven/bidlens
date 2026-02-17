import datetime as dt
import json
from typing import Any, Dict, Optional, Tuple
from .sam_client import search_opportunities
from sqlalchemy.orm import Session
from .models import Opportunity
import time


time.sleep(0.25)

def ingest_sam(db: Session, naics_list: list[str], days_back: int = 7):
    results = []
    for naics in naics_list:
        try:
            result = pull_sam_into_db(db, naics=naics, days_back=days_back)
            results.append(result)
        except Exception as e:
            results.append({"naics": naics, "error": str(e)})
    return results



def _parse_date(s: Optional[str]) -> Optional[dt.date]:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        return None

def normalize_sam_record(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    SAM record -> dict matching Opportunity columns.
    Returns None if required fields are missing.
    """

    sam_notice_id = rec.get("noticeId") or rec.get("noticeID") or rec.get("id")

    title = rec.get("title") or rec.get("solicitationTitle") or rec.get("fullTitle")
    agency = rec.get("department") or rec.get("organizationName") or rec.get("fullParentPathName")

    opportunity_type = rec.get("type") or rec.get("noticeType") or rec.get("opportunityType")

    posted_date = _parse_date(rec.get("postedDate") or rec.get("publishDate"))
    response_deadline = _parse_date(rec.get("responseDeadLine") or rec.get("responseDeadline"))

    naics = rec.get("naics") or rec.get("naicsCode")
    set_aside = rec.get("typeOfSetAside") or rec.get("setAside") or rec.get("setAsideCode")

    # Try to keep a small description string if present
    description = rec.get("description")
    if description is not None and not isinstance(description, str):
        description = None

    sam_url = rec.get("uiLink") or rec.get("link") or rec.get("resourceLink") or rec.get("description")

    #organization_name = rec.get("organizationName")

    # REQUIRED FIELDS per your model
    if not (sam_notice_id and title and agency and opportunity_type and posted_date and response_deadline and sam_url):
        return None

    return {
        "sam_notice_id": str(sam_notice_id),
        "title": str(title),
        "agency": str(agency),
        "opportunity_type": str(opportunity_type),
        "posted_date": posted_date,
        "response_deadline": response_deadline,
        "naics": str(naics) if naics else None,
        "set_aside": str(set_aside) if set_aside else None,
        "description": description,
        "sam_url": str(sam_url),
    }

def upsert_opportunity(db: Session, data: Dict[str, Any]) -> str:
    existing = (
        db.query(Opportunity)
        .filter(Opportunity.sam_notice_id == data["sam_notice_id"])
        .one_or_none()
    )

    if existing is None:
        db.add(Opportunity(**data))
        return "inserted"

    # Update only non-null values (don’t overwrite with None)
    for k, v in data.items():
        if k == "sam_notice_id":
            continue
        if v is not None:
            setattr(existing, k, v)
    return "updated"
def pull_sam_into_db(
    db: Session,
    *,
    naics: str,
    days_back: int = 7,
    limit: int = 100,
    max_pages: int = 20,
) -> Dict[str, Any]:
    """
    Orchestrator for ONE NAICS code:
    - calls SAM API
    - normalizes each record
    - upserts into DB
    - commits
    - returns counts
    """
    today = dt.date.today()
    posted_from = today - dt.timedelta(days=days_back)
    posted_to = today

    inserted = 0
    updated = 0
    skipped = 0
    offset = 0

    for _ in range(max_pages):
        payload = search_opportunities(
            naics=naics,
            posted_from=posted_from,
            posted_to=posted_to,
            limit=limit,
            offset=offset,
        )

        records = payload.get("opportunitiesData") or payload.get("opportunities") or []
        if not records:
            break

        for rec in records:
            data = normalize_sam_record(rec)
            if data is None:
                skipped += 1
                continue

            status = upsert_opportunity(db, data)
            if status == "inserted":
                inserted += 1
            elif status == "updated":
                updated += 1

        db.commit()
        offset += limit

    return {
        "naics": naics,
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
    }
