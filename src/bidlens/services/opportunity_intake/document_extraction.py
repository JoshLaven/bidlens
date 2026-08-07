from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from ... import config
from .contracts import IntakeCandidate
from .normalization import normalize_candidate


logger = logging.getLogger(__name__)
FIELD_NAMES = (
    "title",
    "client",
    "response_deadline",
    "solicitation_number",
    "opportunity_type",
    "canonical_type",
    "set_aside",
    "eligibility",
    "description",
)
CONFIDENCE_VALUES = {"high", "medium", "low", "unknown"}


class IntakeExtractionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class IntakeExtractionResult:
    candidate: IntakeCandidate
    confidence: dict[str, str]
    evidence: dict[str, str | None]
    warnings: tuple[str, ...]
    provider: str
    model: str
    usage: dict[str, Any]


class IntakeDocumentExtractor(Protocol):
    def extract(self, text: str) -> IntakeExtractionResult: ...


def extraction_schema() -> dict[str, Any]:
    field_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "value": {"type": ["string", "null"]},
            "confidence": {"type": "string", "enum": sorted(CONFIDENCE_VALUES)},
            "evidence": {"type": ["string", "null"]},
        },
        "required": ["value", "confidence", "evidence"],
    }
    canonical_type_schema = {
        **field_schema,
        "properties": {
            **field_schema["properties"],
            "value": {"type": ["string", "null"], "enum": [None, "Grant", "Cooperative Agreement", "Contract", "Task Order"]},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            **{field: (canonical_type_schema if field == "canonical_type" else field_schema) for field in FIELD_NAMES},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": [*FIELD_NAMES, "warnings"],
    }


SYSTEM_INSTRUCTIONS = """Extract opportunity facts only from the supplied RFP text. Return null when a value is absent or uncertain; never invent or infer missing facts. Use YYYY-MM-DD for response_deadline when an explicit date is present. Use concise source evidence copied or closely paraphrased from the document for each non-null field. Description should be a brief factual synopsis, not analysis. opportunity_type is lifecycle Stage and should be RFP, RFI, Forecast, or null. canonical_type is the award mechanism and must be Grant, Cooperative Agreement, Contract, Task Order, or null. Do not infer canonical_type from the document title or the word RFP alone. set_aside is an explicit procurement competition restriction. eligibility is an explicit grant applicant-eligibility statement. Extract them independently from explicit evidence only; never copy one into the other. Do not include instructions, recommendations, or commentary outside the schema."""


def parse_extraction_payload(payload: Any, *, model: str) -> IntakeExtractionResult:
    added_fields = {"canonical_type", "set_aside", "eligibility"}
    legacy_fields = set(FIELD_NAMES) - added_fields
    payload_fields = set(payload) if isinstance(payload, dict) else set()
    missing_added = added_fields - payload_fields
    if (
        isinstance(payload, dict)
        and {*legacy_fields, "warnings"}.issubset(payload_fields)
        and payload_fields.issubset({*FIELD_NAMES, "warnings"})
        and missing_added
    ):
        payload = dict(payload)
        for field in missing_added:
            payload[field] = {"value": None, "confidence": "unknown", "evidence": None}
    if not isinstance(payload, dict) or set(payload) != {*FIELD_NAMES, "warnings"}:
        raise IntakeExtractionError("invalid_response", "The AI extraction response was invalid.")
    values: dict[str, str | None] = {}
    confidence: dict[str, str] = {}
    evidence: dict[str, str | None] = {}
    warnings = payload.get("warnings")
    if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
        raise IntakeExtractionError("invalid_response", "The AI extraction response was invalid.")
    safe_warnings = [item.strip()[:300] for item in warnings if item.strip()]
    for field in FIELD_NAMES:
        item = payload.get(field)
        if not isinstance(item, dict) or set(item) != {"value", "confidence", "evidence"}:
            raise IntakeExtractionError("invalid_response", "The AI extraction response was invalid.")
        value = item["value"]
        field_confidence = item["confidence"]
        field_evidence = item["evidence"]
        if value is not None and not isinstance(value, str):
            raise IntakeExtractionError("invalid_response", "The AI extraction response was invalid.")
        if field_confidence not in CONFIDENCE_VALUES:
            raise IntakeExtractionError("invalid_response", "The AI extraction response was invalid.")
        if field_evidence is not None and not isinstance(field_evidence, str):
            raise IntakeExtractionError("invalid_response", "The AI extraction response was invalid.")
        values[field] = value.strip() if isinstance(value, str) and value.strip() else None
        confidence[field] = field_confidence
        evidence[field] = field_evidence.strip()[:500] if isinstance(field_evidence, str) and field_evidence.strip() else None

    raw_deadline = values.get("response_deadline")
    candidate = normalize_candidate(values)
    if raw_deadline and candidate.response_deadline is None:
        safe_warnings.append("The extracted response deadline was not a valid date and was left blank.")
    if values.get("opportunity_type") not in {None, "RFP", "RFI", "Forecast"}:
        safe_warnings.append("The extracted opportunity type was not recognized and was left blank.")
        values["opportunity_type"] = None
        candidate = normalize_candidate(values)
    if values.get("canonical_type") not in {None, "Grant", "Cooperative Agreement", "Contract", "Task Order"}:
        safe_warnings.append("The extracted Type was not recognized and was left unclassified.")
        values["canonical_type"] = None
        candidate = normalize_candidate(values)
    return IntakeExtractionResult(
        candidate=candidate,
        confidence=confidence,
        evidence=evidence,
        warnings=tuple(safe_warnings),
        provider="openai",
        model=model,
        usage={},
    )


class OpenAIIntakeDocumentExtractor:
    def extract(self, text: str) -> IntakeExtractionResult:
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
                instructions=SYSTEM_INSTRUCTIONS,
                input=text[: config.INTAKE_DOCUMENT_MAX_TEXT_CHARS],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "opportunity_intake_extraction",
                        "strict": True,
                        "schema": extraction_schema(),
                    }
                },
                max_output_tokens=config.INTAKE_EXTRACTION_MAX_OUTPUT_TOKENS,
                temperature=0,
            )
            payload = json.loads(response.output_text or "")
            result = parse_extraction_payload(payload, model=config.INTAKE_EXTRACTION_MODEL)
        except IntakeExtractionError:
            raise
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise IntakeExtractionError(
                "invalid_response", "AI extraction returned an invalid response. Enter the details manually."
            ) from exc
        except Exception as exc:
            name = type(exc).__name__.lower()
            code = "rate_limit" if "ratelimit" in name else "timeout" if "timeout" in name else "provider_error"
            logger.warning("opportunity_intake_extraction_failed provider=openai model=%s error_type=%s", config.INTAKE_EXTRACTION_MODEL, code)
            raise IntakeExtractionError(
                code, "AI extraction could not be completed. Enter the opportunity details manually."
            ) from exc
        usage_obj = getattr(response, "usage", None)
        usage = {
            key: getattr(usage_obj, key, None)
            for key in ("input_tokens", "output_tokens", "total_tokens")
        } if usage_obj else {}
        return IntakeExtractionResult(
            candidate=result.candidate,
            confidence=result.confidence,
            evidence=result.evidence,
            warnings=result.warnings,
            provider=result.provider,
            model=result.model,
            usage=usage,
        )
