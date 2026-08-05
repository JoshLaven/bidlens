from __future__ import annotations


DISPLAY_STAGES = ("Forecast", "RFI", "RFP")

GOVWIN_STAGE_MAP = {
    "forecast pre-rfp": "Forecast",
    "pre-rfp": "RFI",
    "post-rfp": "RFP",
}

GOVWIN_EXCLUDED_STAGES = {"source selection"}

RFI_TYPE_INDICATORS = (
    "rfi",
    "request for information",
    "sources sought",
    "special notice",
    "presolicitation",
    "pre-solicitation",
)


def clean_stage(value: str | None) -> str:
    return str(value or "").strip()


def govwin_display_stage(source_stage: str | None) -> str | None:
    return GOVWIN_STAGE_MAP.get(clean_stage(source_stage).casefold())


def is_excluded_govwin_stage(source_stage: str | None) -> bool:
    return clean_stage(source_stage).casefold() in GOVWIN_EXCLUDED_STAGES


def normalize_display_stage(
    *,
    source: str | None,
    opportunity_type: str | None,
    source_stage: str | None = None,
) -> str | None:
    """Return BidLens' compact discovery stage without changing source fidelity."""
    source_value = clean_stage(source).casefold()
    raw_stage = clean_stage(source_stage) or clean_stage(opportunity_type)
    if source_value in {"govwin_export", "govwin_api"}:
        mapped = govwin_display_stage(raw_stage)
        if mapped:
            return mapped

    normalized_type = clean_stage(opportunity_type).casefold()
    normalized_source_stage = clean_stage(source_stage).casefold()
    if source_value in {"grants_gov", "grants.gov"}:
        source_values = {normalized_type, normalized_source_stage}
        if source_values & {"forecast", "forecasted", "forecasted opportunity"}:
            return "Forecast"
        if any(indicator in value for value in source_values for indicator in RFI_TYPE_INDICATORS):
            return "RFI"
        if source_values & {"rfp", "request for proposal", "request for proposals"}:
            return "RFP"
        # Grants.gov's posted synopsis/funding-opportunity concepts are not an
        # RFP. Until BidLens has a broader stage taxonomy, leave them unclassified.
        return None

    if normalized_type == "forecast":
        return "Forecast"
    if any(indicator in normalized_type for indicator in RFI_TYPE_INDICATORS):
        return "RFI"
    return "RFP"
