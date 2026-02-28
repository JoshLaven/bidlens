import datetime as dt
from typing import Any, Dict, Optional, Set

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from .sam_client import search_opportunities
from .models import Opportunity, IngestionRun

ALLOWED_TYPES = {
    "Solicitation",
    "Combined Synopsis/Solicitation",
    "Sources Sought",
    "Special Notice",
    "RFI",
    "Presolicitation",
}


def parse_allowed_types(s: str | None) -> set[str]:
    if not s:
        return set()
    return {x.strip() for x in s.split(",") if x.strip()}


def ingest_sam(
    db: Session,
    naics_list: list[str],
    days_back: int = 7,
    allowed_types: Optional[Set[str]] = None,
):
    allowed_types = allowed_types or set()

    # Start run record
    run = IngestionRun(source="sam.gov")
    db.add(run)
    db.commit()
    db.refresh(run)

    inserted = skipped = filtered = errors = 0
    results: list[dict[str, Any]] = []

    for naics in naics_list:
        try:
            result = pull_sam_into_db(
                db,
                naics=naics,
                days_back=days_back,
                allowed_types=allowed_types,
                ingestion_run_id=run.id,
            )

            inserted += int(result.get("inserted", 0))
            skipped += int(result.get("skipped", 0))
            filtered += int(result.get("filtered", 0))
            errors += int(result.get("errors", 0))

            results.append(result)

        except Exception as e:
            errors += 1
            results.append({"naics": naics, "error": str(e), "inserted": 0, "skipped": 0, "filtered": 0, "errors": 1})

    # Finish run record
    run.inserted_count = inserted
    run.skipped_count = skipped
    run.filtered_count = filtered
    run.error_count = errors
    run.finished_at = dt.datetime.utcnow()  # <-- you had datetime.utcnow() but imported dt

    db.commit()

    return {
        "run_id": run.id,
        "inserted": inserted,
        "skipped": skipped,
        "filtered": filtered,
        "errors": errors,
        "results": results,
    }


def _parse_date(s: Optional[str]) -> Optional[dt.date]:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        return None


def normalize_sam_record(rec: Dict[str, Any], allowed_types: Set[str]) -> Optional[Dict[str, Any]]:
    """
    SAM record -> dict matching Opportunity columns.
    Returns None if required fields are missing or filtered out.
    """

    sam_notice_id = rec.get("noticeId") or rec.get("noticeID") or rec.get("id")

    title = rec.get("title") or rec.get("solicitationTitle") or rec.get("fullTitle")
    agency = rec.get("department") or rec.get("organizationName") or rec.get("fullParentPathName")

    opportunity_type = rec.get("type") or rec.get("noticeType") or rec.get("opportunityType")

    # Type filter: if allowed_types provided, enforce it; otherwise fall back to ALLOWED_TYPES
    if allowed_types:
        if opportunity_type not in allowed_types:
            return None
    else:
        if opportunity_type not in ALLOWED_TYPES:
            return None

    posted_date = _parse_date(rec.get("postedDate") or rec.get("publishDate"))
    response_deadline = _parse_date(rec.get("responseDeadLine") or rec.get("responseDeadline"))

    naics = rec.get("naics") or rec.get("naicsCode")
    set_aside = rec.get("typeOfSetAside") or rec.get("setAside") or rec.get("setAsideCode")

    description = rec.get("description")
    if description is not None and not isinstance(description, str):
        description = None

    sam_url = rec.get("uiLink") or rec.get("link") or rec.get("resourceLink")
    # NOTE: you had `or rec.get("description")` here, which can accidentally put huge text in sam_url

    # REQUIRED fields per model
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
    """
    Returns: "inserted" | "updated" | "skipped"
    Uses DB uniqueness on sam_notice_id; safe under concurrency.
    """
    existing = (
        db.query(Opportunity)
        .filter(Opportunity.sam_notice_id == data["sam_notice_id"])
        .one_or_none()
    )

    if existing is None:
        db.add(Opportunity(**data, upserted_at=dt.datetime.utcnow()))
        try:
            db.flush()  # flush so we catch IntegrityError before committing page
            return "inserted"
        except IntegrityError:
            db.rollback()
            return "skipped"

    # Update only non-null values (don’t overwrite with None)
    changed = False
    for k, v in data.items():
        if k == "sam_notice_id":
            continue
        if v is not None and getattr(existing, k) != v:
            setattr(existing, k, v)
            changed = True

    if changed:
        existing.upserted_at = dt.datetime.utcnow()
        return "updated"

    return "skipped"


def pull_sam_into_db(
    db: Session,
    *,
    naics: str,
    days_back: int = 7,
    limit: int = 100,
    max_pages: int = 20,
    allowed_types: Optional[Set[str]] = None,
    ingestion_run_id: int | None = None,
) -> Dict[str, Any]:
    allowed_types = allowed_types or set()

    today = dt.date.today()
    posted_from = today - dt.timedelta(days=days_back)
    posted_to = today  # <-- you referenced posted_to but never defined it
    offset = 0         # <-- you referenced offset but never defined it

    inserted = 0
    updated = 0
    skipped = 0
    filtered = 0
    errors = 0
    pulled = 0

    for _page in range(max_pages):
        try:
            payload = search_opportunities(
                naics=naics,
                posted_from=posted_from,
                posted_to=posted_to,
                limit=limit,
                offset=offset,
            )
        except Exception as e:
            print("[SAM ERROR]", "naics=", naics, "offset=", offset, "error=", repr(e))
            raise

        records = payload.get("opportunitiesData") or payload.get("opportunities") or []
        pulled += len(records)
        print(f"[SAM] naics={naics} offset={offset} got_records={len(records)} keys={list(payload.keys())[:10]}")
        if not records:
            break

        for rec in records:
            try:
                data = normalize_sam_record(rec, allowed_types)
                if data is None:
                    filtered += 1
                    continue

                status = upsert_opportunity(db, data)
                if status == "inserted":
                    inserted += 1
                elif status == "updated":
                    updated += 1
                else:
                    skipped += 1

            except Exception as e:
                errors += 1
                print("[RECORD ERROR]", "naics=", naics, "sam_notice_id=", rec.get("noticeId"), "err=", repr(e))
                # Keep going; we want ingestion to be resilient

        # Commit per page (good balance of safety + speed)
        try:
            db.commit()
        except Exception:
            db.rollback()
            errors += 1

        offset += limit

    return {
        "naics": naics,
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "filtered": filtered,
        "errors": errors,
        "ingestion_run_id": ingestion_run_id,
        "pulled":pulled
    }