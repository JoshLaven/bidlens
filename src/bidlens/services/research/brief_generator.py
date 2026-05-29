from __future__ import annotations

import json
import logging
import re
from typing import Any

from openai import OpenAI

from ...config import OPENAI_API_KEY, OPENAI_MODEL
from ...models import Opportunity
from ...sam_client import _is_url_like
from .document_fetcher import (
    MAX_INPUT_TOKENS_ESTIMATE,
    MAX_TOTAL_CHARS,
    fetch_opportunity_documents,
)


MAX_DESCRIPTION_CHARS = 12000
MAX_DOCUMENT_CHARS_PER_FILE = 30000
PROMPT_CHAR_BUDGET = min(MAX_TOTAL_CHARS, MAX_INPUT_TOKENS_ESTIMATE * 4)
BRIEF_SECTION_ORDER = [
    ("executive_summary", "Executive Summary"),
    ("key_dates", "Key Dates"),
    ("buyer_agency", "Buyer / Agency"),
    ("scope_of_work", "Scope of Work"),
    ("eligibility_set_aside", "Eligibility / Set-Aside"),
    ("submission_requirements", "Submission Requirements"),
    ("evaluation_criteria", "Evaluation Criteria"),
    ("fit_signals", "Fit Signals"),
    ("risk_flags", "Risk Flags"),
    ("open_questions", "Open Questions"),
    ("recommended_action", "Recommended Action"),
]
logger = logging.getLogger(__name__)
OPENAI_PROVIDER = "openai"
SOURCE_TEXT_FIELD_ORDER = (
    "description",
    "description_text",
    "notice_description",
    "raw_description",
)


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _estimate_tokens_from_chars(char_count: int) -> int:
    return max(1, (char_count + 3) // 4) if char_count > 0 else 0


def build_opportunity_source_text(
    opportunity: Opportunity,
    *,
    brief_context: str | None = None,
) -> tuple[str, str | None]:
    for field_name in SOURCE_TEXT_FIELD_ORDER:
        value = getattr(opportunity, field_name, None)
        text = (value or "").strip() if isinstance(value, str) else ""
        if text and not _is_url_like(text):
            return text, field_name

    context_text = (brief_context or "").strip()
    if context_text:
        return context_text, "brief_context"

    return "", None


def _not_found_list() -> list[str]:
    return ["Not found in available materials"]


def _render_brief_instructions() -> str:
    section_labels = "\n".join(f"- {label}" for _, label in BRIEF_SECTION_ORDER)
    schema_lines = "\n".join(f'  "{key}": ["bullet 1", "bullet 2"]' for key, _ in BRIEF_SECTION_ORDER)
    return "\n".join(
        [
            "You are generating a BidLens opportunity brief for bid/no-bid triage.",
            "Use concise, plain-English, decision-oriented language.",
            "Return a JSON object only.",
            "Use solicitation document text as the primary source of truth when it is available.",
            "Use the SAM description only as supplemental context.",
            "Prefer exact extracted requirements, instructions, dates, and evaluation language over generic summaries.",
            "Every section below is required, even if the answer is missing.",
            "If information is missing, write exactly: Not found in available materials",
            "Do not guess or infer facts that are not stated in the SAM description or solicitation documents.",
            "Use short bullet-style strings in each array.",
            "Required sections:",
            section_labels,
            "Return this JSON shape:",
            "{",
            schema_lines,
            "}",
        ]
    )


def _brief_schema() -> dict[str, Any]:
    properties = {
        key: {
            "type": "array",
            "items": {"type": "string"},
            "description": f"Concise bullet points for {label}.",
        }
        for key, label in BRIEF_SECTION_ORDER
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [key for key, _ in BRIEF_SECTION_ORDER],
        "properties": properties,
    }


def _render_document_sections(documents: list[dict[str, Any]]) -> str:
    if not documents:
        return "No readable PDF solicitation documents were retrieved."

    sections = []
    for document in documents:
        sections.append(
            "\n".join(
                [
                    f"Filename: {document['filename']}",
                    f"Source URL: {document['source_url']}",
                    f"Extracted Text:\n{document['extracted_text']}",
                ]
            )
        )
    return "\n\n---\n\n".join(sections)


def _render_attachment_metadata(
    documents: list[dict[str, Any]],
    source_summary: dict[str, Any],
) -> str:
    filenames = [doc["filename"] for doc in documents if doc.get("filename")]
    lines = [
        f"Attachments found on SAM: {source_summary.get('total_attachments_found', 0)}",
        f"PDFs processed: {source_summary.get('pdfs_processed', 0)}",
        f"Pages extracted: {source_summary.get('pages_extracted', 0)}",
        f"Characters read: {source_summary.get('total_extracted_characters', 0)}",
    ]
    if filenames:
        lines.append("Document filenames reviewed: " + "; ".join(filenames))
    else:
        lines.append("Document filenames reviewed: None")
    return "\n".join(lines)


def _build_text_for_brief(
    opportunity: Opportunity,
    *,
    description: str,
    documents: list[dict[str, Any]],
    source_summary: dict[str, Any],
) -> str:
    due_date = opportunity.response_deadline.strftime("%B %d, %Y") if opportunity.response_deadline else "Unknown"
    prompt_sections = [
        f"Title: {opportunity.title}",
        f"Agency: {opportunity.agency}",
        f"Due Date: {due_date}",
        "Primary Solicitation Document Text:\n" + _render_document_sections(documents),
        "Supplemental SAM Description:\n" + (description or "No SAM description text available."),
        "Attachment Metadata:\n" + _render_attachment_metadata(documents, source_summary),
        "Brief Instructions:\n" + _render_brief_instructions(),
    ]
    return "\n\n".join(prompt_sections)


def build_brief_request_payload(opportunity: Opportunity) -> dict[str, Any]:
    source_text, source_text_field = build_opportunity_source_text(opportunity)
    description = _truncate(source_text, MAX_DESCRIPTION_CHARS)
    fetch_result = fetch_opportunity_documents(opportunity)
    fetched_documents = fetch_result["documents"]
    source_summary = fetch_result["summary"]

    documents: list[dict[str, Any]] = []
    remaining_chars = max(0, PROMPT_CHAR_BUDGET - len(description))
    for document in fetched_documents:
        if remaining_chars <= 0:
            logger.info("Prompt char cap reached before adding filename=%s", document["filename"])
            break
        extracted_text = _truncate(document["extracted_text"], min(MAX_DOCUMENT_CHARS_PER_FILE, remaining_chars))
        remaining_chars -= len(extracted_text)
        documents.append(
            {
                **document,
                "extracted_text": extracted_text,
            }
        )

    filenames_processed = [doc["filename"] for doc in documents]
    source_basis = "solicitation_documents" if documents else "description_only"

    sources_used: list[dict[str, Any]] = []
    if description:
        sources_used.append(
            {
                "type": "sam_description",
                "label": "SAM description",
                "url": opportunity.sam_url,
            }
        )
    for doc in documents:
        sources_used.append(
            {
                "type": "solicitation_pdf",
                "label": doc["filename"],
                "url": doc["source_url"],
            }
        )

    text_for_brief = _build_text_for_brief(
        opportunity,
        description=description,
        documents=documents,
        source_summary=source_summary,
    )
    input_chars = len(text_for_brief)
    estimated_input_tokens = _estimate_tokens_from_chars(input_chars)
    document_chars_sent = sum(len(doc.get("extracted_text", "")) for doc in documents)
    attachment_metadata_text = _render_attachment_metadata(documents, source_summary)
    if estimated_input_tokens > MAX_INPUT_TOKENS_ESTIMATE:
        logger.warning(
            "Brief payload estimated tokens exceed V1 target opp_id=%s estimated_tokens=%s limit=%s",
            opportunity.id,
            estimated_input_tokens,
            MAX_INPUT_TOKENS_ESTIMATE,
        )
    logger.info(
        "Brief payload ready opp_id=%s input_chars=%s estimated_input_tokens=%s docs_included=%s source_text_field=%s description_length=%s document_chars_sent=%s attachment_metadata_chars=%s",
        opportunity.id,
        input_chars,
        estimated_input_tokens,
        len(documents),
        source_text_field,
        len(description),
        document_chars_sent,
        len(attachment_metadata_text),
    )
    source_summary["attachments_found"] = source_summary.get("total_attachments_found", 0)
    source_summary["document_filenames"] = filenames_processed
    source_summary["input_chars"] = input_chars
    source_summary["estimated_input_tokens"] = estimated_input_tokens
    source_summary["prompt_char_budget"] = PROMPT_CHAR_BUDGET
    source_summary["characters_sent_to_model"] = input_chars
    source_summary["description_characters_sent"] = len(description)
    source_summary["document_characters_sent"] = document_chars_sent
    source_summary["attachment_metadata_characters_sent"] = len(attachment_metadata_text)

    return {
        "opp_id": opportunity.id,
        "id": opportunity.id,
        "title": opportunity.title,
        "agency": opportunity.agency,
        "opportunity_type": opportunity.opportunity_type,
        "posted_date": opportunity.posted_date.isoformat() if opportunity.posted_date else None,
        "response_deadline": opportunity.response_deadline.isoformat() if opportunity.response_deadline else None,
        "naics": opportunity.naics,
        "set_aside": opportunity.set_aside,
        "url": opportunity.sam_url,
        "description": description,
        "source_text": source_text,
        "source_text_field": source_text_field,
        "solicitation_documents": documents,
        "sources_used": sources_used,
        "filenames_processed": filenames_processed,
        "source_basis": source_basis,
        "source_summary": source_summary,
        "used_solicitation_documents": bool(documents),
        "prompt_instructions": _render_brief_instructions(),
        "desired_brief_schema": {key: [] for key, _ in BRIEF_SECTION_ORDER},
        "text_for_brief": text_for_brief,
        "text_for_enrichment": text_for_brief,
    }


def _split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [part.strip(" -•\n\t") for part in parts if len(part.strip()) > 30]


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        normalized = item.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(item.strip())
    return out


def _pick_sentences(sentences: list[str], patterns: tuple[str, ...], limit: int) -> list[str]:
    matched: list[str] = []
    for sentence in sentences:
        lowered = sentence.lower()
        if any(pattern in lowered for pattern in patterns):
            matched.append(sentence)
        if len(matched) >= limit:
            break
    return _dedupe_preserve_order(matched)[:limit]


def _fallback_summary(sentences: list[str], limit: int = 3) -> list[str]:
    return _dedupe_preserve_order(sentences[:limit])


def generate_local_brief(opportunity: Opportunity, payload: dict[str, Any]) -> dict[str, Any]:
    description = (payload.get("description") or "").strip()
    document_text = "\n\n".join(
        doc.get("extracted_text", "").strip()
        for doc in payload.get("solicitation_documents", [])
        if doc.get("extracted_text")
    ).strip()
    combined = "\n\n".join(part for part in [description, document_text] if part).strip()

    sentences = _split_sentences(combined)

    summary_bullets = _fallback_summary(sentences, limit=3)

    requirement_patterns = (
        "shall",
        "must",
        "required",
        "requirement",
        "experience",
        "qualification",
        "capability",
        "security clearance",
    )
    deliverable_patterns = (
        "deliver",
        "deliverable",
        "submit",
        "submission",
        "provide",
        "report",
        "transcription",
        "service",
    )
    eligibility_patterns = (
        "small business",
        "set-aside",
        "eligible",
        "eligibility",
        "socioeconomic",
        "vendor",
    )
    red_flag_patterns = (
        "past performance",
        "security clearance",
        "on-site",
        "onsite",
        "oral presentation",
        "transition",
        "urgent",
        "mandatory",
    )

    submission_requirements = _pick_sentences(sentences, requirement_patterns, 5)
    scope_of_work = _pick_sentences(sentences, deliverable_patterns, 5)
    eligibility = _pick_sentences(sentences, eligibility_patterns, 4)
    risk_flags = _pick_sentences(sentences, red_flag_patterns, 4)
    evaluation_criteria = _pick_sentences(
        sentences,
        ("evaluation", "criteria", "best value", "award", "technical", "past performance", "price"),
        4,
    )

    if opportunity.set_aside:
        eligibility = _dedupe_preserve_order(
            [f"Set-aside: {opportunity.set_aside}"] + eligibility
        )[:4]

    recommended_action: list[str] = []
    if opportunity.response_deadline:
        recommended_action.append(
            f"Confirm the response plan and internal owners before the {opportunity.response_deadline.strftime('%B %d, %Y')} deadline."
        )
    if payload.get("used_solicitation_documents"):
        recommended_action.append("Review the solicitation documents first, then decide whether to advance, based on scope, compliance requirements, and evaluation approach.")
    else:
        recommended_action.append("Review the SAM.gov notice directly and confirm whether additional attachments need review before advancing.")
    if opportunity.naics:
        recommended_action.append(f"Verify fit against NAICS {opportunity.naics} and any related capability statements.")
    if opportunity.set_aside:
        recommended_action.append(f"Confirm eligibility for the {opportunity.set_aside} set-aside before advancing.")

    if not summary_bullets:
        summary_bullets = [
            f"{opportunity.title} is a {opportunity.opportunity_type.lower()} opportunity from {opportunity.agency}.",
            "The available brief was generated from limited source text, so the SAM notice should be reviewed directly.",
        ]

    key_dates: list[str] = []
    if opportunity.posted_date:
        key_dates.append(f"Posted date: {opportunity.posted_date.strftime('%B %d, %Y')}")
    if opportunity.response_deadline:
        key_dates.append(f"Response deadline: {opportunity.response_deadline.strftime('%B %d, %Y')}")
    if not key_dates:
        key_dates = _not_found_list()

    buyer_agency = [opportunity.agency] if opportunity.agency else _not_found_list()

    fit_signals: list[str] = []
    if opportunity.naics:
        fit_signals.append(f"NAICS: {opportunity.naics}")
    if opportunity.set_aside:
        fit_signals.append(f"Set-aside: {opportunity.set_aside}")
    if payload.get("used_solicitation_documents"):
        fit_signals.append("Solicitation documents were retrieved and reviewed.")
    if not fit_signals:
        fit_signals = _not_found_list()

    open_questions = _pick_sentences(
        sentences,
        ("question", "clarify", "unclear", "tbd", "to be determined", "unknown"),
        4,
    )

    return {
        "executive_summary": summary_bullets[:4] or _not_found_list(),
        "key_dates": key_dates[:4],
        "buyer_agency": buyer_agency[:4],
        "scope_of_work": scope_of_work[:5] or _not_found_list(),
        "eligibility_set_aside": eligibility[:4] or _not_found_list(),
        "submission_requirements": submission_requirements[:5] or _not_found_list(),
        "evaluation_criteria": evaluation_criteria[:4] or _not_found_list(),
        "fit_signals": fit_signals[:5] or _not_found_list(),
        "risk_flags": risk_flags[:4] or _not_found_list(),
        "open_questions": open_questions[:4] or _not_found_list(),
        "recommended_action": _dedupe_preserve_order(recommended_action)[:5] or _not_found_list(),
    }


def generate_llm_brief(payload: dict[str, Any]) -> dict[str, Any]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    client = OpenAI(api_key=OPENAI_API_KEY)
    model = OPENAI_MODEL
    input_text = payload.get("text_for_brief") or payload["text_for_enrichment"]
    input_chars = len(input_text)

    response = client.responses.create(
        model=model,
        input=input_text,
        text={
            "format": {
                "type": "json_schema",
                "name": "bidlens_brief",
                "strict": True,
                "schema": _brief_schema(),
            }
        },
        max_output_tokens=2200,
    )

    output_text = response.output_text or ""
    output_chars = len(output_text)
    if not output_text.strip():
        raise RuntimeError("OpenAI returned an empty brief response")

    parsed = json.loads(output_text)

    normalized: dict[str, list[str]] = {}
    for key, _label in BRIEF_SECTION_ORDER:
        values = parsed.get(key)
        items = _normalize_section_list(values)
        normalized[key] = items or _not_found_list()

    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "input_tokens", None) if usage else None
    output_tokens = getattr(usage, "output_tokens", None) if usage else None
    total_tokens = getattr(usage, "total_tokens", None) if usage else None
    estimated_input_tokens = _estimate_tokens_from_chars(input_chars)

    logger.info(
        "OpenAI brief generated model=%s input_chars=%s output_chars=%s estimated_input_tokens=%s input_tokens=%s output_tokens=%s total_tokens=%s fallback_triggered=%s",
        model,
        input_chars,
        output_chars,
        estimated_input_tokens,
        input_tokens,
        output_tokens,
        total_tokens,
        False,
    )

    return {
        "provider": OPENAI_PROVIDER,
        "model": model,
        "brief": normalized,
        "usage": {
            "input_chars": input_chars,
            "output_chars": output_chars,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        },
    }


def _normalize_section_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    return [str(value).strip()] if str(value).strip() else []
