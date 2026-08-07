from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


PROCUREMENT_TYPES = frozenset({"Contract", "Task Order"})
ASSISTANCE_TYPES = frozenset({"Grant", "Cooperative Agreement"})

_SAM_SET_ASIDE_CODES = {
    "SBA": "Total Small Business",
    "SBP": "Partial Small Business",
    "8A": "8(a)",
    "8AN": "8(a)",
    "HZC": "HUBZone",
    "SDVOSBC": "SDVOSB",
    "WOSB": "WOSB",
    "EDWOSB": "EDWOSB",
}


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).strip().split())
    return text or None


def _first(payload: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = _clean(payload.get(key))
        if value:
            return value
    return None


def _applicant_type_descriptions(payload: Mapping[str, Any]) -> str | None:
    """Return distinct structured applicant types in their source-defined order."""
    applicant_types = payload.get("applicantTypes")
    if not isinstance(applicant_types, list):
        return None

    descriptions: list[str] = []
    seen: set[str] = set()
    for applicant_type in applicant_types:
        if not isinstance(applicant_type, Mapping):
            continue
        description = _clean(applicant_type.get("description"))
        if not description:
            continue
        normalized = description.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        descriptions.append(description)
    return "; ".join(descriptions) or None


def sam_set_aside(payload: Mapping[str, Any]) -> str | None:
    """Return structured SAM set-aside description, mapped code, or raw value."""
    description = _first(payload, ("typeOfSetAsideDescription", "setAsideDescription"))
    if description:
        return description
    raw = _first(payload, ("typeOfSetAside", "setAside", "setAsideCode"))
    if not raw:
        return None
    return _SAM_SET_ASIDE_CODES.get(raw.upper(), raw)


def grants_eligibility(payload: Mapping[str, Any]) -> str | None:
    """Select one structured Grants.gov applicant-eligibility value deterministically."""
    synopsis = payload.get("synopsis")
    synopsis = synopsis if isinstance(synopsis, Mapping) else {}
    forecast = payload.get("forecast")
    forecast = forecast if isinstance(forecast, Mapping) else {}
    return (
        _applicant_type_descriptions(synopsis)
        or _applicant_type_descriptions(forecast)
        or _applicant_type_descriptions(payload)
        or _first(synopsis, ("applicantEligibilityDesc",))
        or _first(payload, ("applicantEligibilityDesc",))
        or _first(synopsis, ("additionalInformationOnEligibility",))
        or _first(payload, ("additionalInformationOnEligibility",))
    )


def govwin_set_aside(payload: Mapping[str, Any]) -> str | None:
    return _first(payload, ("set_aside", "setAside", "set_aside_description", "setAsideDescription"))


def govwin_eligibility(payload: Mapping[str, Any]) -> str | None:
    return _first(payload, ("eligibility", "applicant_eligibility", "applicantEligibility"))


@dataclass(frozen=True)
class QualificationPresentation:
    label: str
    value: str
    preview: str
    is_long: bool


def qualification_presentation(opportunity: Any, *, preview_limit: int = 220) -> QualificationPresentation | None:
    """Return the one Type-appropriate qualification concept for UI presentation."""
    canonical_type = _clean(getattr(opportunity, "canonical_type", None))
    if canonical_type in PROCUREMENT_TYPES:
        value = _clean(getattr(opportunity, "set_aside", None)) or "Not specified"
        return QualificationPresentation("Set-Aside", value, value, False)
    if canonical_type in ASSISTANCE_TYPES:
        value = _clean(getattr(opportunity, "eligibility", None)) or "Not specified"
        is_long = len(value) > preview_limit
        preview = value if not is_long else value[:preview_limit].rstrip() + "…"
        return QualificationPresentation("Eligibility", value, preview, is_long)
    return None
