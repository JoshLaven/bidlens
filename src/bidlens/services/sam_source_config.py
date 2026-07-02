from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Any

from ..models import SamSourceConfig


SAM_NOTICE_TYPES = (
    "Solicitation",
    "Combined Synopsis/Solicitation",
    "Sources Sought",
    "Special Notice",
    "RFI",
    "Presolicitation",
)
MAX_SAM_RECORDS = 1000
MAX_DATE_WINDOW_DAYS = 365


class SamConfigValidationError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("Invalid SAM.gov source configuration")
        self.errors = errors


def parse_multi_value(value: str | None) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[\n,]+", value or ""):
        cleaned = item.strip()
        key = cleaned.casefold()
        if cleaned and key not in seen:
            values.append(cleaned)
            seen.add(key)
    return values


def _integer(
    value: str | int | None,
    *,
    field: str,
    label: str,
    minimum: int,
    maximum: int,
    required: bool,
    errors: dict[str, str],
) -> int | None:
    if value is None or str(value).strip() == "":
        if required:
            errors[field] = f"{label} is required."
        return None
    try:
        parsed = int(str(value).strip())
    except ValueError:
        errors[field] = f"{label} must be a whole number."
        return None
    if not minimum <= parsed <= maximum:
        errors[field] = f"{label} must be between {minimum} and {maximum}."
    return parsed


def validate_sam_config_input(
    *,
    search_name: str | None = None,
    naics_codes: str | None,
    keywords: str | None,
    agencies: str | None,
    set_asides: str | None,
    notice_types: list[str] | tuple[str, ...] | None,
    posted_days_back: str | int | None,
    due_days_from: str | int | None,
    due_days_to: str | int | None,
    active_only: bool,
    max_records: str | int | None,
) -> dict[str, Any]:
    errors: dict[str, str] = {}
    parsed_name = (search_name or "").strip()
    if search_name is not None and not parsed_name:
        errors["search_name"] = "Search name is required."
    elif len(parsed_name) > 120:
        errors["search_name"] = "Search name must be 120 characters or fewer."
    parsed_naics = parse_multi_value(naics_codes)
    if not parsed_naics:
        errors["naics_codes"] = "Enter at least one NAICS code."
    elif invalid := [
        code for code in parsed_naics
        if not code.isdigit() or not 2 <= len(code) <= 6
    ]:
        errors["naics_codes"] = (
            "NAICS codes must contain 2–6 digits: "
            + ", ".join(invalid)
        )

    parsed_types = list(dict.fromkeys(notice_types or []))
    invalid_types = [value for value in parsed_types if value not in SAM_NOTICE_TYPES]
    if invalid_types:
        errors["notice_types"] = "Select only supported SAM.gov notice types."

    posted = _integer(
        posted_days_back,
        field="posted_days_back",
        label="Posted date window",
        minimum=1,
        maximum=MAX_DATE_WINDOW_DAYS,
        required=True,
        errors=errors,
    )
    due_from = _integer(
        due_days_from,
        field="due_days_from",
        label="Due date start",
        minimum=0,
        maximum=MAX_DATE_WINDOW_DAYS,
        required=False,
        errors=errors,
    )
    due_to = _integer(
        due_days_to,
        field="due_days_to",
        label="Due date end",
        minimum=0,
        maximum=MAX_DATE_WINDOW_DAYS,
        required=False,
        errors=errors,
    )
    if due_from is not None and due_to is not None and due_from > due_to:
        errors["due_days_to"] = "Due date end must be on or after the due date start."

    maximum = _integer(
        max_records,
        field="max_records",
        label="Max records",
        minimum=1,
        maximum=MAX_SAM_RECORDS,
        required=True,
        errors=errors,
    )
    if errors:
        raise SamConfigValidationError(errors)

    values = {
        "naics_codes": parsed_naics,
        "keywords": parse_multi_value(keywords),
        "agencies": parse_multi_value(agencies),
        "set_asides": parse_multi_value(set_asides),
        "notice_types": parsed_types,
        "posted_days_back": posted,
        "due_days_from": due_from,
        "due_days_to": due_to,
        "active_only": bool(active_only),
        "max_records": maximum,
    }
    if search_name is not None:
        values["name"] = parsed_name
    return values


def config_form_values(config: SamSourceConfig | None) -> dict[str, Any]:
    if config is None:
        return {
            "search_name": "",
            "naics_codes": "",
            "keywords": "",
            "agencies": "",
            "set_asides": "",
            "notice_types": [],
            "posted_days_back": 30,
            "due_days_from": "",
            "due_days_to": "",
            "active_only": True,
            "max_records": 100,
        }
    return {
        "search_name": config.name,
        "naics_codes": "\n".join(config.naics_codes or []),
        "keywords": "\n".join(config.keywords or []),
        "agencies": "\n".join(config.agencies or []),
        "set_asides": "\n".join(config.set_asides or []),
        "notice_types": list(config.notice_types or []),
        "posted_days_back": config.posted_days_back,
        "due_days_from": config.due_days_from if config.due_days_from is not None else "",
        "due_days_to": config.due_days_to if config.due_days_to is not None else "",
        "active_only": bool(config.active_only),
        "max_records": config.max_records,
    }


@lru_cache(maxsize=1)
def naics_catalog() -> list[dict[str, str]]:
    path = Path(__file__).resolve().parent.parent / "data" / "naics_2022.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("codes") or [])


def ingest_kwargs(config: SamSourceConfig) -> dict[str, Any]:
    return {
        "naics_list": list(config.naics_codes or []),
        "days_back": config.posted_days_back,
        "allowed_types": set(config.notice_types or []),
        "keywords": set(config.keywords or []),
        "agencies": set(config.agencies or []),
        "set_asides": set(config.set_asides or []),
        "due_days_from": config.due_days_from,
        "due_days_to": config.due_days_to,
        "active_only": bool(config.active_only),
        "max_records": config.max_records,
    }
