from __future__ import annotations

from typing import Any, Mapping


CANONICAL_OPPORTUNITY_TYPES = (
    "Grant",
    "Cooperative Agreement",
    "Contract",
    "Task Order",
)

_TYPE_LOOKUP = {value.casefold(): value for value in CANONICAL_OPPORTUNITY_TYPES}


def normalize_canonical_type(value: Any) -> str | None:
    """Return an approved canonical Type without inferring semantic aliases."""
    if value is None:
        return None
    normalized = " ".join(str(value).strip().split()).casefold()
    return _TYPE_LOOKUP.get(normalized)


def _first_mapping_value(payload: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, "", [], {}):
            return value
    lowered = {str(key).casefold(): value for key, value in payload.items()}
    for key in keys:
        value = lowered.get(key.casefold())
        if value not in (None, "", [], {}):
            return value
    return None


def _instrument_value(value: Any) -> Any:
    if isinstance(value, list):
        return _instrument_value(value[0]) if len(value) == 1 else None
    if isinstance(value, Mapping):
        return _first_mapping_value(
            value,
            ("description", "name", "value", "fundingInstrumentDescription"),
        )
    return value


def grants_canonical_type(payload: Mapping[str, Any]) -> str | None:
    """Map Grants.gov Funding Instrument Type using structured fields only.

    Precedence: top-level descriptive field, top-level instrument field, synopsis,
    then forecast. Ambiguous/multiple instruments remain unclassified.
    """
    containers = (payload, payload.get("synopsis"), payload.get("forecast"))
    keys = (
        "fundingInstrumentDescription",
        "fundingInstrumentDesc",
        "fundingInstrumentType",
        "fundingInstrument",
        "fundingInstruments",
    )
    for container in containers:
        if not isinstance(container, Mapping):
            continue
        value = _instrument_value(_first_mapping_value(container, keys))
        mapped = normalize_canonical_type(value)
        if mapped in {"Grant", "Cooperative Agreement"}:
            return mapped
        if value not in (None, "", [], {}):
            return None
    return None


def sam_canonical_type(payload: Mapping[str, Any]) -> str:
    """SAM notices are procurement opportunities; no reliable task-order field exists."""
    return "Contract"


def govwin_api_canonical_type(payload: Mapping[str, Any]) -> str | None:
    """Map explicit GovWin API award-mechanism fields, never lifecycle stage."""
    return normalize_canonical_type(_first_mapping_value(
        payload,
        ("award_mechanism", "awardMechanism", "award_type", "awardType", "contract_mechanism", "contractMechanism"),
    ))


def govwin_spreadsheet_canonical_type(row: Mapping[str, Any]) -> str | None:
    """Map explicit export columns; the legacy Type column remains stage-only."""
    return normalize_canonical_type(_first_mapping_value(
        row,
        ("Award Mechanism", "Award Type", "Contract Mechanism"),
    ))
