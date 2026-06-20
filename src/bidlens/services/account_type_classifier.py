from __future__ import annotations

from dataclasses import dataclass
import re


ACCOUNT_TYPES = {"Federal", "State Government", "Regional Government", "Nonprofit University"}
ACCOUNT_TYPE_CONFIDENCES = {"high", "medium", "low"}
ACCOUNT_TYPE_SOURCES = {"rule", "ai", "manual", "unknown"}

US_STATES = {
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "california",
    "colorado",
    "connecticut",
    "delaware",
    "florida",
    "georgia",
    "hawaii",
    "idaho",
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "minnesota",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "new hampshire",
    "new jersey",
    "new mexico",
    "new york",
    "north carolina",
    "north dakota",
    "ohio",
    "oklahoma",
    "oregon",
    "pennsylvania",
    "rhode island",
    "south carolina",
    "south dakota",
    "tennessee",
    "texas",
    "utah",
    "vermont",
    "virginia",
    "washington",
    "west virginia",
    "wisconsin",
    "wyoming",
    "district of columbia",
}

FEDERAL_PHRASES = {
    "centers for medicare and medicaid services",
    "centers for medicare & medicaid services",
    "department of defense",
    "department of health and human services",
    "department of veterans affairs",
    "naval supply systems command",
    "national institutes of health",
    "centers for disease control",
    "centers for disease control and prevention",
    "u.s. department",
    "us department",
    "united states department",
}

FEDERAL_ACRONYMS = {"hhs", "cms", "nih", "cdc", "usda", "va", "dod"}


@dataclass(frozen=True)
class AccountTypeClassification:
    account_type: str | None
    confidence: str
    source: str
    reason: str


def _normalize(value: str | None) -> str:
    text = re.sub(r"[^a-z0-9&.]+", " ", str(value or "").lower()).strip()
    return re.sub(r"\s+", " ", text)


def _has_word(text: str, word: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(word.lower())}(?![a-z0-9])", text))


def _starts_with_state(text: str) -> str | None:
    for state in sorted(US_STATES, key=len, reverse=True):
        if text == state or text.startswith(f"{state} "):
            return state
    return None


def classify_account_type(account_name: str | None) -> AccountTypeClassification:
    text = _normalize(account_name)
    if not text:
        return AccountTypeClassification(None, "low", "unknown", "missing_account_name")

    if any(phrase in text for phrase in ("board of regents", "university", "college")):
        return AccountTypeClassification("Nonprofit University", "high", "rule", "university_or_college")

    if any(phrase in text for phrase in FEDERAL_PHRASES):
        return AccountTypeClassification("Federal", "high", "rule", "known_federal_phrase")
    if any(_has_word(text, acronym) for acronym in FEDERAL_ACRONYMS):
        return AccountTypeClassification("Federal", "high", "rule", "known_federal_acronym")
    if "united states" in text or "u.s." in text:
        return AccountTypeClassification("Federal", "high", "rule", "federal_indicator")

    if "state of" in text:
        return AccountTypeClassification("State Government", "high", "rule", "state_of")
    state = _starts_with_state(text)
    if state:
        confidence = "medium" if "department of" in text else "high"
        return AccountTypeClassification("State Government", confidence, "rule", "starts_with_state_name")

    regional_patterns = (
        "city of",
        "town of",
        "village of",
        "borough of",
        "municipality",
        "school district",
        "public schools",
        "unified school district",
        "transit authority",
    )
    if any(pattern in text for pattern in regional_patterns):
        return AccountTypeClassification("Regional Government", "high", "rule", "regional_government_pattern")
    if re.search(r",\s*city of\b", text):
        return AccountTypeClassification("Regional Government", "high", "rule", "govwin_inverted_city")
    if _has_word(text, "county"):
        return AccountTypeClassification("Regional Government", "high", "rule", "county")
    if _has_word(text, "district"):
        return AccountTypeClassification("Regional Government", "medium", "rule", "district")

    return AccountTypeClassification(None, "low", "unknown", "no_rule_match")
