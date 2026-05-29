import datetime as dt
import logging
import threading
from typing import Any, Dict, Optional, Set

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from .sam_client import SamRateLimitError, SamTemporaryUnavailableError, resolve_notice_description, search_opportunities
from .models import Opportunity, IngestionRun

logger = logging.getLogger(__name__)
_INGEST_LOCK = threading.Lock()

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


def sam_ingest_in_progress() -> bool:
    return _INGEST_LOCK.locked()


def ingest_sam(
    db: Session,
    naics_list: list[str],
    days_back: int = 7,
    allowed_types: Optional[Set[str]] = None,
    manual_pull: bool = False,
    enrich_descriptions: bool = False,
    max_description_enrichments: int = 10,
):
    allowed_types = allowed_types or set()

    if not _INGEST_LOCK.acquire(blocking=False):
        raise RuntimeError("A SAM pull is already in progress")

    try:
        run = IngestionRun(source="sam.gov")
        db.add(run)
        db.commit()
        db.refresh(run)

        inserted = updated = skipped = filtered = errors = 0
        pages_pulled = 0
        records_seen = 0
        search_requests_made = 0
        results: list[dict[str, Any]] = []
        stopped_due_to_rate_limit = False
        rate_limit_retry_after_seconds: float | None = None
        rate_limit_retry_after: str | None = None

        logger.info(
            "Starting SAM ingest run_id=%s naics_count=%s days_back=%s allowed_types=%s",
            run.id,
            len(naics_list),
            days_back,
            sorted(allowed_types) if allowed_types else "default",
        )

        for index, naics in enumerate(naics_list):
            try:
                result = pull_sam_into_db(
                    db,
                    naics=naics,
                    days_back=days_back,
                    allowed_types=allowed_types,
                    ingestion_run_id=run.id,
                    allow_rate_limit_wait=not manual_pull,
                    enrich_descriptions=enrich_descriptions,
                    max_description_enrichments=max_description_enrichments,
                )

                inserted += int(result.get("inserted", 0))
                updated += int(result.get("updated", 0))
                skipped += int(result.get("skipped", 0))
                filtered += int(result.get("filtered", 0))
                errors += int(result.get("errors", 0))
                pages_pulled += int(result.get("pages_pulled", 0))
                records_seen += int(result.get("records_seen", 0))
                search_requests_made += int(result.get("search_requests_made", 0))

                results.append(result)
                logger.info(
                    "Completed SAM NAICS naics=%s pages_pulled=%s records_seen=%s search_requests=%s inserted=%s updated=%s skipped=%s filtered=%s errors=%s pulled=%s",
                    naics,
                    result.get("pages_pulled", 0),
                    result.get("records_seen", 0),
                    result.get("search_requests_made", 0),
                    result.get("inserted", 0),
                    result.get("updated", 0),
                    result.get("skipped", 0),
                    result.get("filtered", 0),
                    result.get("errors", 0),
                    result.get("pulled", 0),
                )
            except Exception as e:
                db.rollback()
                errors += 1
                retry_after_seconds = e.retry_after_seconds if isinstance(e, SamRateLimitError) else None
                retry_after = e.retry_after if isinstance(e, SamRateLimitError) else None
                naics_result = {
                    "naics": naics,
                    "error": str(e),
                    "error_type": (
                        "rate_limited" if isinstance(e, SamRateLimitError)
                        else "sam_unavailable" if isinstance(e, SamTemporaryUnavailableError)
                        else "exception"
                    ),
                    "retry_after_seconds": retry_after_seconds,
                    "retry_after": retry_after,
                    "inserted": 0,
                    "updated": 0,
                    "skipped": 0,
                    "filtered": 0,
                    "errors": 1,
                    "pulled": 0,
                    "pages_pulled": 0,
                    "records_seen": 0,
                    "search_requests_made": 0,
                }
                results.append(naics_result)
                logger.exception("SAM NAICS failed naics=%s error=%s", naics, repr(e))

                if isinstance(e, SamRateLimitError):
                    stopped_due_to_rate_limit = True
                    rate_limit_retry_after_seconds = retry_after_seconds
                    rate_limit_retry_after = retry_after
                    remaining_naics = [item.strip() for item in naics_list[index + 1:] if item.strip()]
                    for remaining_naics_code in remaining_naics:
                        results.append({
                            "naics": remaining_naics_code,
                            "error": "Skipped because SAM.gov rate limited the manual pull.",
                            "error_type": "rate_limited_skipped",
                            "retry_after_seconds": retry_after_seconds,
                            "retry_after": retry_after,
                            "inserted": 0,
                            "updated": 0,
                            "skipped": 0,
                            "filtered": 0,
                            "errors": 0,
                            "pulled": 0,
                            "pages_pulled": 0,
                            "records_seen": 0,
                            "search_requests_made": 0,
                        })
                    logger.warning(
                        "Stopping SAM ingest after rate limit run_id=%s naics=%s remaining_naics=%s manual_pull=%s retry_after=%s retry_after_seconds=%s",
                        run.id,
                        naics,
                        remaining_naics,
                        manual_pull,
                        retry_after,
                        retry_after_seconds,
                    )
                    break

        run.inserted_count = inserted + updated
        run.skipped_count = skipped
        run.filtered_count = filtered
        run.error_count = errors
        run.finished_at = dt.datetime.utcnow()
        run.notes = (
            f"inserted={inserted} updated={updated} skipped={skipped} "
            f"filtered={filtered} errors={errors} pages={pages_pulled} "
            f"records_seen={records_seen} search_requests={search_requests_made}"
        )

        db.commit()

        if stopped_due_to_rate_limit and inserted == 0 and updated == 0 and skipped == 0 and filtered == 0:
            status = "rate_limited"
        elif errors == 0:
            status = "success"
        elif inserted == 0 and updated == 0 and skipped == 0 and filtered == 0:
            status = "failed"
        else:
            status = "partial_success"
        run.notes = f"status={status} {run.notes}"
        db.commit()
        logger.info(
            "Finished SAM ingest run_id=%s status=%s pages=%s records_seen=%s search_requests=%s inserted=%s updated=%s skipped=%s filtered=%s errors=%s",
            run.id,
            status,
            pages_pulled,
            records_seen,
            search_requests_made,
            inserted,
            updated,
            skipped,
            filtered,
            errors,
        )

        return {
            "status": status,
            "run_id": run.id,
            "inserted": inserted,
            "updated": updated,
            "skipped": skipped,
            "filtered": filtered,
            "errors": errors,
            "pages_pulled": pages_pulled,
            "records_seen": records_seen,
            "search_requests_made": search_requests_made,
            "results": results,
            "stopped_due_to_rate_limit": stopped_due_to_rate_limit,
            "retry_after_seconds": rate_limit_retry_after_seconds,
            "retry_after": rate_limit_retry_after,
        }
    finally:
        _INGEST_LOCK.release()


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

    description = (
        rec.get("description")
        or rec.get("descriptionText")
        or rec.get("noticeDescription")
        or rec.get("rawDescription")
        or rec.get("synopsis")
        or rec.get("additionalInfo")
    )
    if description is not None and not isinstance(description, str):
        description = None
    description = description.strip() if isinstance(description, str) else None

    sam_url = rec.get("uiLink") or rec.get("link") or rec.get("resourceLink")
    # NOTE: you had `or rec.get("description")` here, which can accidentally put huge text in sam_url

    # REQUIRED fields per model
    if not (sam_notice_id and title and agency and opportunity_type and posted_date and response_deadline and sam_url):
        return None

    description_url = description if _description_needs_fetch(description) else None
    description_text = None if description_url else description

    return {
        "sam_notice_id": str(sam_notice_id),
        "title": str(title),
        "agency": str(agency),
        "opportunity_type": str(opportunity_type),
        "posted_date": posted_date,
        "response_deadline": response_deadline,
        "naics": str(naics) if naics else None,
        "set_aside": str(set_aside) if set_aside else None,
        "description": description_text,
        "description_url": description_url,
        "description_text": description_text,
        "sam_url": str(sam_url),
    }


def _description_needs_fetch(description: str | None) -> bool:
    return bool(description) and description.strip().lower().startswith("http")


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
        try:
            with db.begin_nested():
                db.add(Opportunity(**data, upserted_at=dt.datetime.utcnow()))
                db.flush()
            return "inserted"
        except IntegrityError:
            logger.info("Skipping duplicate SAM notice sam_notice_id=%s", data["sam_notice_id"])
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
    allow_rate_limit_wait: bool = True,
    enrich_descriptions: bool = False,
    max_description_enrichments: int = 10,
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
    pages_pulled = 0
    search_requests_made = 0
    description_enrichments = 0

    for _page in range(max_pages):
        try:
            payload = search_opportunities(
                naics=naics,
                posted_from=posted_from,
                posted_to=posted_to,
                limit=limit,
                offset=offset,
                allow_rate_limit_wait=allow_rate_limit_wait,
            )
            search_requests_made += 1
        except Exception as e:
            logger.exception("SAM page fetch failed naics=%s offset=%s error=%s", naics, offset, repr(e))
            raise

        records = payload.get("opportunitiesData") or payload.get("opportunities") or []
        pulled += len(records)
        pages_pulled += 1
        logger.info(
            "Fetched SAM page naics=%s offset=%s records=%s",
            naics,
            offset,
            len(records),
        )
        if not records:
            break

        for rec in records:
            try:
                data = normalize_sam_record(rec, allowed_types)
                if data is None:
                    filtered += 1
                    continue

                if enrich_descriptions and data.get("description_url") and description_enrichments < max_description_enrichments:
                    try:
                        resolved_description = resolve_notice_description(
                            data["description_url"],
                            data.get("sam_url"),
                        )
                    except SamRateLimitError:
                        raise
                    except Exception as exc:
                        logger.warning(
                            "SAM notice description fetch failed naics=%s sam_notice_id=%s error=%s",
                            naics,
                            data["sam_notice_id"],
                            repr(exc),
                        )
                        resolved_description = None

                    if resolved_description:
                        data["description"] = resolved_description
                        data["description_text"] = resolved_description
                    description_enrichments += 1
                elif data.get("description_url") and not enrich_descriptions:
                    logger.debug(
                        "Skipping inline description enrichment naics=%s sam_notice_id=%s enrich_descriptions=%s",
                        naics,
                        data["sam_notice_id"],
                        enrich_descriptions,
                    )
                elif data.get("description_url") and description_enrichments >= max_description_enrichments:
                    logger.info(
                        "Skipping inline description enrichment due to cap naics=%s sam_notice_id=%s cap=%s",
                        naics,
                        data["sam_notice_id"],
                        max_description_enrichments,
                    )

                status = upsert_opportunity(db, data)
                if status == "inserted":
                    inserted += 1
                elif status == "updated":
                    updated += 1
                else:
                    skipped += 1

            except Exception as e:
                errors += 1
                logger.exception(
                    "SAM record failed naics=%s sam_notice_id=%s error=%s",
                    naics,
                    rec.get("noticeId") or rec.get("noticeID") or rec.get("id"),
                    repr(e),
                )

        try:
            db.commit()
        except Exception as e:
            db.rollback()
            errors += 1
            logger.exception(
                "SAM page commit failed naics=%s offset=%s error=%s",
                naics,
                offset,
                repr(e),
            )

        offset += limit

    return {
        "naics": naics,
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "filtered": filtered,
        "errors": errors,
        "ingestion_run_id": ingestion_run_id,
        "pulled": pulled,
        "pages_pulled": pages_pulled,
        "records_seen": pulled,
        "search_requests_made": search_requests_made,
        "description_enrichments": description_enrichments,
        "enrich_descriptions": enrich_descriptions,
    }


def backfill_opportunity_descriptions(
    db: Session,
    *,
    limit: int = 25,
) -> Dict[str, Any]:
    checked = 0
    updated = 0
    skipped = 0
    errors = 0
    rate_limited = 0
    results: list[dict[str, Any]] = []

    rows = (
        db.query(Opportunity)
        .filter(
            Opportunity.description_url.is_not(None),
            Opportunity.description_url != "",
        )
        .order_by(Opportunity.id.asc())
        .limit(limit)
        .all()
    )

    for opp in rows:
        checked += 1

        if opp.description_text and opp.description_text.strip():
            skipped += 1
            results.append({
                "opp_id": opp.id,
                "status": "skipped",
                "reason": "description_text already present",
            })
            continue

        try:
            resolved = resolve_notice_description(opp.description_url, opp.sam_url)
        except SamRateLimitError as exc:
            db.rollback()
            errors += 1
            rate_limited += 1
            results.append({
                "opp_id": opp.id,
                "status": "rate_limited",
                "retry_after_seconds": exc.retry_after_seconds,
                "error": str(exc),
            })
            break
        except Exception as exc:
            db.rollback()
            errors += 1
            results.append({
                "opp_id": opp.id,
                "status": "error",
                "error": str(exc),
            })
            continue

        if resolved:
            opp.description_text = resolved
            if opp.description and not opp.description.strip().lower().startswith("http"):
                pass
            elif not opp.description:
                opp.description = resolved
            db.commit()
            updated += 1
            results.append({
                "opp_id": opp.id,
                "status": "updated",
            })
        else:
            skipped += 1
            results.append({
                "opp_id": opp.id,
                "status": "skipped",
                "reason": "no readable description resolved",
            })

    return {
        "checked": checked,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "rate_limited": rate_limited,
        "results": results,
    }
