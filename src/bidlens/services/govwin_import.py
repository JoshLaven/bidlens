from __future__ import annotations

import datetime as dt
import re
import zipfile
from collections import Counter
from io import BytesIO
from typing import Any
from xml.etree import ElementTree as ET

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Opportunity
from .account_type_classifier import classify_account_type
from .qualification import new_opportunity_qualification_status
from .pursuit_lanes import refresh_opportunity_lane_matches


SOURCE = "govwin_export"
NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pkgrel": "http://schemas.openxmlformats.org/package/2006/relationships",
}

REQUIRED_COLUMNS = ("Title", "GovWin Staging Name", "GovEntity Title")
DATE_COLUMNS = {"Created Date", "Response Date", "Solicitation Date", "GW Update Date", "Update Date"}
SAM_OPP_URL_RE = re.compile(r"/opp/([^/?#]+)/", re.IGNORECASE)
REASON_LABELS = {
    "new_opportunity": "New opportunity",
    "existing_govwin_record_changed": "Existing GovWin record changed",
    "existing_govwin_record": "Existing GovWin record",
    "cross_source_sam_notice_match_enriched": "Cross-source SAM Notice ID match enriched",
    "cross_source_sam_notice_match": "Cross-source SAM Notice ID match",
    "missing_title": "Missing Title",
    "missing_govwin_staging_name": "Missing GovWin Staging Name",
    "missing_goventity_title": "Missing GovEntity Title",
    "missing_usable_date": "Missing usable Response Date, Created Date, or Solicitation Date",
    "integrity_error": "Duplicate or integrity error",
}


def _cell_ref_to_index(ref: str) -> int:
    letters = "".join(ch for ch in ref if ch.isalpha()).upper()
    value = 0
    for ch in letters:
        value = value * 26 + (ord(ch) - ord("A") + 1)
    return max(0, value - 1)


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext())


def _load_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return [_text(item) for item in root.findall("main:si", NS)]


def _first_sheet_path(archive: zipfile.ZipFile) -> str:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    first_sheet = workbook.find("main:sheets/main:sheet", NS)
    if first_sheet is None:
        raise ValueError("Workbook has no worksheets")
    rel_id = first_sheet.attrib.get(f"{{{NS['rel']}}}id")
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    for rel in rels.findall("pkgrel:Relationship", NS):
        if rel.attrib.get("Id") == rel_id:
            target = rel.attrib.get("Target", "")
            return "xl/" + target.lstrip("/")
    raise ValueError("Could not resolve first worksheet")


def _date_style_indexes(archive: zipfile.ZipFile) -> set[int]:
    if "xl/styles.xml" not in archive.namelist():
        return set()
    root = ET.fromstring(archive.read("xl/styles.xml"))
    custom_date_numfmts: set[int] = set()
    for numfmt in root.findall("main:numFmts/main:numFmt", NS):
        try:
            numfmt_id = int(numfmt.attrib.get("numFmtId", ""))
        except ValueError:
            continue
        code = numfmt.attrib.get("formatCode", "").lower()
        if any(token in code for token in ("yy", "mm", "dd", "date")):
            custom_date_numfmts.add(numfmt_id)

    builtin_date_numfmts = set(range(14, 23)) | {27, 30, 36, 45, 46, 47, 50, 57}
    date_numfmts = builtin_date_numfmts | custom_date_numfmts
    date_styles: set[int] = set()
    cell_xfs = root.find("main:cellXfs", NS)
    if cell_xfs is None:
        return date_styles
    for index, xf in enumerate(cell_xfs.findall("main:xf", NS)):
        try:
            numfmt_id = int(xf.attrib.get("numFmtId", "0"))
        except ValueError:
            numfmt_id = 0
        if numfmt_id in date_numfmts:
            date_styles.add(index)
    return date_styles


def _excel_date(value: str) -> dt.date | None:
    try:
        serial = float(value)
    except (TypeError, ValueError):
        return None
    if serial <= 0:
        return None
    # Excel's 1900 date system includes the historic leap-year bug.
    return (dt.datetime(1899, 12, 30) + dt.timedelta(days=serial)).date()


def _parse_date(value: Any) -> dt.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, (int, float)):
        return _excel_date(str(value))
    text = str(value).strip()
    if not text:
        return None
    excel_date = _excel_date(text)
    if excel_date:
        return excel_date
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _cell_value(cell: ET.Element, shared_strings: list[str], date_styles: set[int]) -> Any:
    cell_type = cell.attrib.get("t")
    style_index = int(cell.attrib.get("s", "0") or 0)

    if cell_type == "inlineStr":
        return _text(cell.find("main:is", NS)).strip()

    raw = _text(cell.find("main:v", NS)).strip()
    if cell_type == "s":
        try:
            return shared_strings[int(raw)].strip()
        except (ValueError, IndexError):
            return ""
    if cell_type == "b":
        return raw == "1"
    if style_index in date_styles:
        return _excel_date(raw) or raw
    return raw


def parse_xlsx_rows(file_bytes: bytes) -> list[dict[str, Any]]:
    with zipfile.ZipFile(BytesIO(file_bytes)) as archive:
        shared_strings = _load_shared_strings(archive)
        date_styles = _date_style_indexes(archive)
        sheet_path = _first_sheet_path(archive)
        root = ET.fromstring(archive.read(sheet_path))

        parsed_rows: list[list[Any]] = []
        for row in root.findall(".//main:sheetData/main:row", NS):
            values: list[Any] = []
            for cell in row.findall("main:c", NS):
                index = _cell_ref_to_index(cell.attrib.get("r", ""))
                while len(values) <= index:
                    values.append("")
                values[index] = _cell_value(cell, shared_strings, date_styles)
            parsed_rows.append(values)

    if not parsed_rows:
        return []
    headers = [str(value or "").strip() for value in parsed_rows[0]]
    rows: list[dict[str, Any]] = []
    for values in parsed_rows[1:]:
        row = {header: values[index] if index < len(values) else "" for index, header in enumerate(headers) if header}
        if any(str(value or "").strip() for value in row.values()):
            rows.append(row)
    return rows


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _raw_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, (dt.date, dt.datetime)):
            payload[key] = value.isoformat()
        else:
            payload[key] = value
    return payload


def extract_sam_notice_id_from_url(value: str | None) -> str | None:
    text = _clean(value)
    if not text or "sam.gov" not in text.lower():
        return None
    match = SAM_OPP_URL_RE.search(text)
    if not match:
        return None
    notice_id = match.group(1).strip()
    return notice_id or None


def _normalize_for_match(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _normalize_row(row: dict[str, Any], row_number: int) -> tuple[dict[str, Any] | None, str | None]:
    title = _clean(row.get("Title"))
    staging_name = _clean(row.get("GovWin Staging Name"))
    agency = _clean(row.get("GovEntity Title"))
    if not title:
        return None, "missing_title"
    if not staging_name:
        return None, "missing_govwin_staging_name"
    if not agency:
        return None, "missing_goventity_title"

    response_deadline = _parse_date(row.get("Response Date"))
    created_date = _parse_date(row.get("Created Date"))
    solicitation_date = _parse_date(row.get("Solicitation Date"))
    if not (response_deadline or created_date or solicitation_date):
        return None, "missing_usable_date"

    posted_date = created_date or solicitation_date or response_deadline
    response_deadline = response_deadline or solicitation_date or created_date
    payload = _raw_payload(row)
    source_url = _clean(row.get("Source URL"))
    sam_notice_id = extract_sam_notice_id_from_url(source_url)
    account_type = classify_account_type(agency)
    payload["_bidlens_import"] = {
        "source": SOURCE,
        "row_number": row_number,
        "extracted_sam_notice_id": sam_notice_id,
        "account_type_reason": account_type.reason,
    }

    return {
        "source": SOURCE,
        "source_record_id": staging_name,
        "govwin_staging_id": _clean(row.get("GovWin Staging ID")),
        "solicitation_number": _clean(row.get("Solicitation Number")),
        "source_url": source_url,
        "raw_source_payload": payload,
        "title": title,
        "agency": agency,
        # Existing model requires opportunity_type; GovWin exports often omit a
        # type column, so use a generic display-compatible value for V1.
        "opportunity_type": _clean(row.get("Type")) or "Solicitation",
        "posted_date": posted_date,
        "response_deadline": response_deadline,
        "naics": _clean(row.get("Primary NAICS Id")),
        "naics_title": _clean(row.get("Primary NAICS Title")),
        "set_aside": None,
        "account_type": account_type.account_type,
        "account_type_confidence": account_type.confidence,
        "account_type_source": account_type.source,
        "description": _clean(row.get("GW Description")),
        "description_url": None,
        "description_text": _clean(row.get("GW Description")),
        "sam_notice_id": sam_notice_id,
        "sam_url": None,
    }, None


def _apply_govwin_cross_source_metadata(existing: Opportunity, data: dict[str, Any]) -> bool:
    """Attach safe GovWin metadata to an existing canonical opportunity.

    SAM Notice ID is authoritative for cross-source identity, but source-native
    fields from the existing opportunity should remain intact.
    """
    changed = False

    def set_if_empty(key: str) -> None:
        nonlocal changed
        value = data.get(key)
        if value is not None and not getattr(existing, key):
            setattr(existing, key, value)
            changed = True

    for key in ("govwin_staging_id", "solicitation_number", "naics", "naics_title"):
        set_if_empty(key)

    if existing.account_type_source != "manual":
        for key in ("account_type", "account_type_confidence", "account_type_source"):
            value = data.get(key)
            if value is not None and getattr(existing, key) != value:
                setattr(existing, key, value)
                changed = True

    return changed


def _cross_source_sam_match_diagnostic(data: dict[str, Any], existing: Opportunity) -> dict[str, Any]:
    sam_notice_id = data.get("sam_notice_id")
    return {
        "opportunity_id": existing.id,
        "source_record_id": data.get("source_record_id"),
        "matched_opportunity_id": existing.id,
        "matched_source": existing.source,
        "matched_source_record_id": existing.source_record_id,
        "matched_sam_notice_id": existing.sam_notice_id,
        "matched_solicitation_number": existing.solicitation_number,
        "reasons": [
            f"same authoritative SAM Notice ID {sam_notice_id}",
            "GovWin duplicate not created",
        ],
    }


def _find_existing_by_sam_notice_id(db: Session, organization_id: int, sam_notice_id: str | None) -> Opportunity | None:
    if not sam_notice_id:
        return None
    return (
        db.query(Opportunity)
        .filter(
            Opportunity.organization_id == organization_id,
            Opportunity.sam_notice_id == sam_notice_id,
        )
        .order_by((Opportunity.source == "sam").desc(), Opportunity.id.asc())
        .first()
    )


def upsert_govwin_opportunity(
    db: Session,
    organization_id: int,
    data: dict[str, Any],
) -> tuple[str, Opportunity | None, dict[str, Any] | None, str]:
    existing = (
        db.query(Opportunity)
        .filter(
            Opportunity.organization_id == organization_id,
            Opportunity.source == data["source"],
            Opportunity.source_record_id == data["source_record_id"],
        )
        .one_or_none()
    )

    if existing is None:
        existing_by_sam_notice = _find_existing_by_sam_notice_id(
            db,
            organization_id,
            data.get("sam_notice_id"),
        )
        if existing_by_sam_notice is not None:
            changed = _apply_govwin_cross_source_metadata(existing_by_sam_notice, data)
            if changed:
                existing_by_sam_notice.upserted_at = dt.datetime.utcnow()
                refresh_opportunity_lane_matches(db, organization_id, existing_by_sam_notice)
            diagnostic = _cross_source_sam_match_diagnostic(data, existing_by_sam_notice)
            reason = "cross_source_sam_notice_match_enriched" if changed else "cross_source_sam_notice_match"
            return ("updated" if changed else "unchanged"), existing_by_sam_notice, diagnostic, reason

        try:
            with db.begin_nested():
                opportunity = Opportunity(
                    organization_id=organization_id,
                    **data,
                    qualification_status=new_opportunity_qualification_status(db, organization_id),
                    upserted_at=dt.datetime.utcnow(),
                )
                db.add(opportunity)
                db.flush()
                refresh_opportunity_lane_matches(db, organization_id, opportunity)
            return "created", opportunity, None, "new_opportunity"
        except IntegrityError:
            return "skipped", None, None, "integrity_error"

    changed = False
    for key, value in data.items():
        if key in {"source", "source_record_id"}:
            continue
        if key in {"account_type", "account_type_confidence", "account_type_source"} and existing.account_type_source == "manual":
            continue
        if value is not None and getattr(existing, key) != value:
            setattr(existing, key, value)
            changed = True
        elif key == "raw_source_payload" and getattr(existing, key) != value:
            setattr(existing, key, value)
            changed = True

    if changed:
        existing.upserted_at = dt.datetime.utcnow()
        refresh_opportunity_lane_matches(db, organization_id, existing)
        return "updated", existing, None, "existing_govwin_record_changed"
    return "unchanged", existing, None, "existing_govwin_record"


def find_cross_source_duplicate_diagnostics(
    db: Session,
    organization_id: int,
    opportunity: Opportunity,
) -> list[dict[str, Any]]:
    filters = []
    if opportunity.sam_notice_id:
        filters.append(Opportunity.sam_notice_id == opportunity.sam_notice_id)
    if opportunity.solicitation_number:
        filters.append(Opportunity.solicitation_number == opportunity.solicitation_number)
    if opportunity.response_deadline:
        filters.append(Opportunity.response_deadline == opportunity.response_deadline)
    if not filters:
        return []

    candidates = (
        db.query(Opportunity)
        .filter(
            Opportunity.organization_id == organization_id,
            Opportunity.id != opportunity.id,
            Opportunity.source != opportunity.source,
            or_(*filters),
        )
        .limit(50)
        .all()
    )
    diagnostics: list[dict[str, Any]] = []
    normalized_title = _normalize_for_match(opportunity.title)
    normalized_agency = _normalize_for_match(opportunity.agency)
    for candidate in candidates:
        reasons: list[str] = []
        if opportunity.sam_notice_id and candidate.sam_notice_id == opportunity.sam_notice_id:
            reasons.append(f"same SAM Notice ID {opportunity.sam_notice_id}")
        if opportunity.solicitation_number and candidate.solicitation_number == opportunity.solicitation_number:
            reasons.append(f"same solicitation number {opportunity.solicitation_number}")
        if (
            opportunity.response_deadline
            and candidate.response_deadline == opportunity.response_deadline
            and _normalize_for_match(candidate.title) == normalized_title
            and _normalize_for_match(candidate.agency) == normalized_agency
        ):
            reasons.append("same normalized title, agency, and response deadline")
        if not reasons:
            continue
        diagnostics.append(
            {
                "opportunity_id": opportunity.id,
                "source_record_id": opportunity.source_record_id,
                "matched_opportunity_id": candidate.id,
                "matched_source": candidate.source,
                "matched_source_record_id": candidate.source_record_id,
                "matched_sam_notice_id": candidate.sam_notice_id,
                "matched_solicitation_number": candidate.solicitation_number,
                "reasons": reasons,
            }
        )
    return diagnostics


def import_govwin_xlsx(db: Session, organization_id: int, file_bytes: bytes) -> dict[str, Any]:
    rows = parse_xlsx_rows(file_bytes)
    reason_counts: Counter[str] = Counter()
    result = {
        "processed": len(rows),
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        "skipped_reasons": [],
        "duplicate_diagnostics": [],
        "reason_counts": {},
        "reason_labels": REASON_LABELS,
    }
    for index, row in enumerate(rows, start=2):
        normalized, reason = _normalize_row(row, index)
        if reason:
            reason_counts[reason] += 1
            result["skipped"] += 1
            result["skipped_reasons"].append({
                "row": index,
                "reason": REASON_LABELS.get(reason, reason),
                "reason_code": reason,
            })
            continue
        status, opportunity, sam_match_diagnostic, reason_code = upsert_govwin_opportunity(db, organization_id, normalized)
        reason_counts[reason_code] += 1
        if status == "created":
            result["created"] += 1
        elif status == "updated":
            result["updated"] += 1
        elif status == "unchanged":
            result["unchanged"] += 1
        else:
            result["skipped"] += 1
            result["skipped_reasons"].append({
                "row": index,
                "reason": REASON_LABELS.get(reason_code, "Duplicate or integrity error"),
                "reason_code": reason_code,
            })
        if sam_match_diagnostic is not None:
            sam_match_diagnostic["row"] = index
            result["duplicate_diagnostics"].append(sam_match_diagnostic)
        if opportunity is not None:
            for diagnostic in find_cross_source_duplicate_diagnostics(db, organization_id, opportunity):
                diagnostic["row"] = index
                result["duplicate_diagnostics"].append(diagnostic)
    result["reason_counts"] = dict(reason_counts)
    return result
