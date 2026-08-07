from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Mapping

from .contracts import DEFAULT_OPPORTUNITY_TYPE, INTAKE_SOURCE, IntakeCandidate
from ..opportunity_types import normalize_canonical_type


_WHITESPACE = re.compile(r"\s+")


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = _WHITESPACE.sub(" ", str(value)).strip()
    return normalized or None


def clean_multiline_text(value: Any) -> str | None:
    if value is None:
        return None
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in str(value).replace("\r", "").split("\n")]
    normalized = "\n".join(lines).strip()
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized or None


def parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = clean_text(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def normalize_candidate(values: IntakeCandidate | Mapping[str, Any]) -> IntakeCandidate:
    if isinstance(values, IntakeCandidate):
        raw: Mapping[str, Any] = values.__dict__
    else:
        raw = values
    canonical_type = normalize_canonical_type(raw.get("canonical_type"))
    set_aside = clean_text(raw.get("set_aside")) if canonical_type in {"Contract", "Task Order"} else None
    eligibility = clean_multiline_text(raw.get("eligibility")) if canonical_type in {"Grant", "Cooperative Agreement"} else None
    return IntakeCandidate(
        title=clean_text(raw.get("title")),
        client=clean_text(raw.get("client")),
        response_deadline=parse_date(raw.get("response_deadline")),
        solicitation_number=clean_text(raw.get("solicitation_number")),
        opportunity_type=clean_text(raw.get("opportunity_type")),
        canonical_type=canonical_type,
        description=clean_multiline_text(raw.get("description")),
        source_url=clean_text(raw.get("source_url")),
        naics=clean_text(raw.get("naics")),
        naics_title=clean_text(raw.get("naics_title")),
        set_aside=set_aside,
        eligibility=eligibility,
    )


def opportunity_creation_defaults(*, saved_on: date) -> dict[str, Any]:
    """Return explicit model defaults for fields omitted from the review form."""
    return {
        "source": INTAKE_SOURCE,
        "posted_date": saved_on,
        "opportunity_type": DEFAULT_OPPORTUNITY_TYPE,
    }


def opportunity_field_values(
    candidate: IntakeCandidate,
    *,
    saved_on: date,
    source_record_id: str,
    solicitation_number: str,
) -> dict[str, Any]:
    """Map the shared review contract onto the existing Opportunity model."""
    values = opportunity_creation_defaults(saved_on=saved_on)
    values.update({
        "source_record_id": source_record_id,
        "solicitation_number": solicitation_number,
        "title": candidate.title,
        "agency": candidate.client,
        "response_deadline": candidate.response_deadline,
        "opportunity_type": candidate.opportunity_type or values["opportunity_type"],
        "canonical_type": candidate.canonical_type,
        "description": candidate.description,
        "description_text": candidate.description,
        "source_url": candidate.source_url,
        "naics": candidate.naics,
        "naics_title": candidate.naics_title,
        "set_aside": candidate.set_aside,
        "eligibility": candidate.eligibility,
    })
    return values
