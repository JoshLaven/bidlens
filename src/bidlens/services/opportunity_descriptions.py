"""Canonical description selection shared with the Opportunity Folder."""

import html
import re

from ..models import Opportunity


def _is_url_like(value: str) -> bool:
    return value.strip().lower().startswith(("http://", "https://"))


def clean_solicitation_description(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(?:p|div|li|tr|h[1-6])\s*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def select_opportunity_description(opportunity: Opportunity) -> str:
    description_text = (opportunity.description_text or "").strip()
    if description_text:
        return clean_solicitation_description(description_text)
    description = (opportunity.description or "").strip()
    if description and not _is_url_like(description):
        return clean_solicitation_description(description)
    return ""
