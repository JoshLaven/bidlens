from __future__ import annotations

from dataclasses import dataclass
import re


GENERIC_AGENCY_SEGMENTS = {
    "agency",
    "department",
    "gov",
    "mil",
    "office",
    "offices",
    "us",
    "usa",
}

GOVERNMENT_ACRONYMS = {
    "cdc": "CDC",
    "cms": "CMS",
    "dod": "DOD",
    "dhs": "DHS",
    "doe": "DOE",
    "dot": "DOT",
    "epa": "EPA",
    "faa": "FAA",
    "gsa": "GSA",
    "hhs": "HHS",
    "nasa": "NASA",
    "nih": "NIH",
    "samhsa": "SAMHSA",
    "usda": "USDA",
    "va": "VA",
}


@dataclass(frozen=True)
class AgencyPresentation:
    raw: str
    display: str
    parent: str | None
    sub_agency: str | None


def _clean_segment(value: str | None) -> str:
    text = str(value or "").replace("_", " ").strip()
    return re.sub(r"\s+", " ", text)


def _display_case(value: str | None) -> str:
    text = _clean_segment(value)
    if not text:
        return ""
    return re.sub(
        r"(?<![A-Za-z0-9])([A-Za-z]{2,6})(?![A-Za-z0-9])",
        lambda match: GOVERNMENT_ACRONYMS.get(match.group(1).lower(), match.group(1).title()),
        text.title(),
    )


def agency_presentation(raw_agency: str | None) -> AgencyPresentation:
    """Return consistent display parts for a source-provided agency value.

    BidLens keeps the raw source agency unchanged in storage. This helper is
    intentionally presentation-only, mirroring the existing card cleanup while
    making the result reusable by templates, exports, and API payloads.
    """

    raw = str(raw_agency or "")
    parts = [
        _clean_segment(part)
        for part in raw.split(".")
        if _clean_segment(part)
    ]
    original = _clean_segment(raw.replace(".", " "))
    original_title = _display_case(original)

    fallback = ""
    candidate = ""
    for part in reversed(parts):
        normalized = part.lower()
        if part and not fallback:
            fallback = part
        if part and not candidate and normalized not in GENERIC_AGENCY_SEGMENTS:
            candidate = part

    candidate_title = _display_case(candidate or fallback)
    display = (
        candidate_title
        if candidate_title and len(candidate_title) >= 4 and len(candidate_title) <= len(original_title)
        else original_title
    )

    parent = _display_case(parts[0]) if parts else None
    sub_agency = _display_case(parts[-1]) if len(parts) > 1 else None
    if sub_agency == parent:
        sub_agency = None

    return AgencyPresentation(
        raw=raw,
        display=display or original_title or raw.strip(),
        parent=parent,
        sub_agency=sub_agency,
    )


def agency_display(raw_agency: str | None) -> str:
    return agency_presentation(raw_agency).display
