from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from ... import config
from .contracts import IntakeCandidate
from .document_extraction import (
    CONFIDENCE_VALUES,
    FIELD_NAMES,
    IntakeExtractionError,
    IntakeExtractionResult,
)
from .normalization import normalize_candidate


logger = logging.getLogger(__name__)
SOURCE_VALUES = {"email", "attachment", "unknown"}


EMAIL_SYSTEM_INSTRUCTIONS = """Extract opportunity facts only from the supplied email intake text. Sections labelled ATTACHMENT contain solicitation documents and are the primary authority when they conflict with informal email wording. EMAIL SUBJECT and EMAIL BODY provide supplemental context. Return null when a value is absent or uncertain and never invent facts. For response_deadline, use YYYY-MM-DD only for an explicit proposal response deadline; do not substitute a question deadline, intent-to-bid date, meeting date, or informational date. Description must be a brief factual synopsis. Opportunity type must be RFP, RFI, Forecast, or null. For each field, identify whether its primary support came from email, attachment, or is unknown. Do not return commentary outside the schema."""


def email_extraction_schema() -> dict[str, Any]:
    def field_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "value": {"type": ["string", "null"]},
                "confidence": {"type": "string", "enum": sorted(CONFIDENCE_VALUES)},
                "evidence": {"type": ["string", "null"]},
                "source": {"type": "string", "enum": sorted(SOURCE_VALUES)},
            },
            "required": ["value", "confidence", "evidence", "source"],
        }

    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            **{field: field_schema() for field in FIELD_NAMES},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": [*FIELD_NAMES, "warnings"],
    }


@dataclass(frozen=True)
class IntakeEmailExtractionResult:
    result: IntakeExtractionResult
    source_attribution: dict[str, str]


class IntakeEmailExtractor(Protocol):
    def extract(self, text: str) -> IntakeEmailExtractionResult: ...


def parse_email_extraction_payload(payload: Any, *, model: str) -> IntakeEmailExtractionResult:
    if not isinstance(payload, dict) or set(payload) != {*FIELD_NAMES, "warnings"}:
        raise IntakeExtractionError("invalid_response", "The AI extraction response was invalid.")
    values: dict[str, str | None] = {}
    confidence: dict[str, str] = {}
    evidence: dict[str, str | None] = {}
    attribution: dict[str, str] = {}
    warnings = payload.get("warnings")
    if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
        raise IntakeExtractionError("invalid_response", "The AI extraction response was invalid.")
    safe_warnings = [item.strip()[:300] for item in warnings if item.strip()]
    for field in FIELD_NAMES:
        item = payload.get(field)
        if not isinstance(item, dict) or set(item) != {"value", "confidence", "evidence", "source"}:
            raise IntakeExtractionError("invalid_response", "The AI extraction response was invalid.")
        value, field_confidence, field_evidence, source = (
            item["value"], item["confidence"], item["evidence"], item["source"]
        )
        if value is not None and not isinstance(value, str):
            raise IntakeExtractionError("invalid_response", "The AI extraction response was invalid.")
        if field_confidence not in CONFIDENCE_VALUES or source not in SOURCE_VALUES:
            raise IntakeExtractionError("invalid_response", "The AI extraction response was invalid.")
        if field_evidence is not None and not isinstance(field_evidence, str):
            raise IntakeExtractionError("invalid_response", "The AI extraction response was invalid.")
        values[field] = value.strip() if isinstance(value, str) and value.strip() else None
        confidence[field] = field_confidence
        evidence[field] = field_evidence.strip()[:500] if isinstance(field_evidence, str) and field_evidence.strip() else None
        attribution[field] = source
    raw_deadline = values.get("response_deadline")
    if values.get("opportunity_type") not in {None, "RFP", "RFI", "Forecast"}:
        safe_warnings.append("The extracted opportunity type was not recognized and was left blank.")
        values["opportunity_type"] = None
    candidate = normalize_candidate(values)
    if raw_deadline and candidate.response_deadline is None:
        safe_warnings.append("The extracted response deadline was not a valid date and was left blank.")
    return IntakeEmailExtractionResult(
        result=IntakeExtractionResult(
            candidate=candidate,
            confidence=confidence,
            evidence=evidence,
            warnings=tuple(safe_warnings),
            provider="openai",
            model=model,
            usage={},
        ),
        source_attribution=attribution,
    )


class OpenAIIntakeEmailExtractor:
    def extract(self, text: str) -> IntakeEmailExtractionResult:
        if not config.OPENAI_API_KEY or not config.INTAKE_EXTRACTION_MODEL:
            raise IntakeExtractionError(
                "missing_configuration",
                "AI extraction is unavailable. Enter the opportunity details manually.",
            )
        from openai import OpenAI

        client = OpenAI(
            api_key=config.OPENAI_API_KEY,
            timeout=config.INTAKE_EXTRACTION_TIMEOUT_SECONDS,
            max_retries=0,
        )
        try:
            response = client.responses.create(
                model=config.INTAKE_EXTRACTION_MODEL,
                instructions=EMAIL_SYSTEM_INSTRUCTIONS,
                input=text[: config.INTAKE_EMAIL_MAX_EXTRACTION_CHARS],
                text={"format": {
                    "type": "json_schema",
                    "name": "opportunity_email_intake_extraction",
                    "strict": True,
                    "schema": email_extraction_schema(),
                }},
                max_output_tokens=config.INTAKE_EXTRACTION_MAX_OUTPUT_TOKENS,
                temperature=0,
            )
            parsed = parse_email_extraction_payload(
                json.loads(response.output_text or ""), model=config.INTAKE_EXTRACTION_MODEL
            )
        except IntakeExtractionError:
            raise
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise IntakeExtractionError(
                "invalid_response", "AI extraction returned an invalid response. Enter the details manually."
            ) from exc
        except Exception as exc:
            name = type(exc).__name__.lower()
            code = "rate_limit" if "ratelimit" in name else "timeout" if "timeout" in name else "provider_error"
            logger.warning(
                "opportunity_email_intake_extraction_failed provider=openai model=%s error_type=%s",
                config.INTAKE_EXTRACTION_MODEL,
                code,
            )
            raise IntakeExtractionError(
                code, "AI extraction could not be completed. Enter the opportunity details manually."
            ) from exc
        usage_obj = getattr(response, "usage", None)
        usage = {
            key: getattr(usage_obj, key, None)
            for key in ("input_tokens", "output_tokens", "total_tokens")
        } if usage_obj else {}
        return IntakeEmailExtractionResult(
            result=IntakeExtractionResult(
                candidate=parsed.result.candidate,
                confidence=parsed.result.confidence,
                evidence=parsed.result.evidence,
                warnings=parsed.result.warnings,
                provider=parsed.result.provider,
                model=parsed.result.model,
                usage=usage,
            ),
            source_attribution=parsed.source_attribution,
        )
