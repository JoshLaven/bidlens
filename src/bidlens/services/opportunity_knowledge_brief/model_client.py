"""Dedicated OpenAI model boundary and one-retry validation flow for GUTS."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
from time import perf_counter
from typing import Any, Protocol

from pydantic import ValidationError

from ... import config
from .contracts import (
    GUTSManifest, ModelBriefingOutput, ProviderBriefingOutputV2, ValidatedBriefingOutput,
)
from .output_normalization import assign_v2_statement_keys
from .output_schema import guts_output_schema, model_probe_output_schema
from .output_validation import GUTSOutputValidator, GUTSValidationError
from .prompt import GUTSPromptConfigurationError, manifest_input, resolve_prompt


logger = logging.getLogger(__name__)
_CITATION_FEEDBACK_PREFIX = "citation_inventory_v1:"
_GROUNDED_FIELD_FEEDBACK_PREFIX = "grounded_field_v1:"
_ATTRIBUTION_FEEDBACK_PREFIX = "attribution_v1:"
_MAX_CITATION_FEEDBACK_CHARACTERS = 16_000
SAFE_RETRY_FEEDBACK = frozenset({
    "Use statement_key 'headline' for the headline.",
    "The headline must use the exact reserved statement_key 'headline'; no alternate, shortened, numbered, or generated key is allowed.",
    "Use high importance for the headline.",
    "Return at least one summary statement.",
    "Return fewer summary statements and sections.",
    "Omit empty sections.",
    "Return fewer statements per section.",
    "Return each section type at most once.",
    "Use a unique statement_key for every statement.",
    "Return a non-blank statement_key for every statement.",
    "Shorten the briefing to at most 500 words.",
    "Return non-blank statement text.",
    "Shorten each statement and keep it atomic.",
    "Use each source ID at most once per statement.",
    "Use only source IDs present in the citation contract.",
    "Keep source IDs only in source_ids arrays.",
    "Remove recommendations, speculation, markup, and raw citation syntax.",
    "Return atomic statements containing one independently supportable idea.",
    "Label organizational claims attributed and cite their organizational sources.",
    "Attributed statements must cite organizational knowledge.",
    "Use wording such as reported, proposed, plans, or noted.",
    "Cite evidence that explicitly expresses the unresolved information.",
    "State supported facts separately without inferring causality.",
    "Place statements only in sections compatible with their source classes.",
    "Use uncertain confidence for every uncertainties statement.",
    "Cite the source containing each exact date.",
    "Cite the current response_deadline source for the operative deadline.",
    "Cite the current solicitation_number source for the identifier.",
})

PROVIDER_ERROR_SUBTYPES = frozenset({
    "model_not_found", "model_access_denied", "unsupported_parameter", "invalid_request",
    "invalid_output_schema", "rate_limited", "quota_exceeded", "authentication_failed",
    "provider_unavailable", "provider_timeout", "unknown_provider_error",
})
_SAFE_PROVIDER_CODES = frozenset({
    "model_not_found", "model_not_available", "model_access_denied",
    "unsupported_parameter", "unsupported_value", "invalid_parameter",
    "invalid_json_schema", "invalid_schema", "schema_validation_error",
    "rate_limit_exceeded", "insufficient_quota", "quota_exceeded",
    "billing_hard_limit_reached", "invalid_api_key", "server_error",
})
_SAFE_PROVIDER_TYPES = frozenset({
    "invalid_request_error", "authentication_error", "permission_error",
    "rate_limit_error", "server_error", "not_found_error",
})
_SAFE_PROVIDER_PARAMS = frozenset({
    "model", "temperature", "max_output_tokens", "text.format",
    "text.format.schema", "response_format", "response_format.json_schema",
})
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SAFE_SCHEMA_PATH_PART = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_SAFE_SCHEMA_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CONTROLLED_ENUM_VALUES = {
    "importance": frozenset({"high", "normal"}),
    "confidence": frozenset({"supported", "attributed", "uncertain"}),
    "section_type": frozenset({
        "current_state", "official_updates", "organizational_knowledge",
        "important_history", "uncertainties",
    }),
}


def supports_temperature(model_name: str) -> bool:
    """Return whether the configured Responses model accepts explicit temperature."""
    normalized = model_name.strip().casefold()
    return not (normalized == "gpt-5.5" or normalized.startswith("gpt-5.5-"))


def build_responses_request(model_name: str, **request: Any) -> dict[str, Any]:
    """Apply deterministic per-model request capabilities before an API call."""
    payload = {"model": model_name, **request}
    if supports_temperature(model_name):
        payload["temperature"] = 0
    return payload


def _allowed_actor_values(manifest: GUTSManifest) -> list[dict[str, Any]]:
    evidence = getattr(manifest, "evidence", None)
    authors = [
        source.author for source in getattr(evidence, "sources", ())
        if source.source_class == "organizational_knowledge" and source.author is not None
    ]
    snapshots: list[dict[str, Any]] = []
    for author in authors:
        snapshot = {
            "user_id": author.user_id,
            "display_name": author.display_name,
            "email": author.address,
        }
        if snapshot not in snapshots:
            snapshots.append(snapshot)
    return snapshots


def _safe_schema_path(location: Any) -> tuple[str, str | None]:
    parts: list[str] = []
    field_name = None
    if not isinstance(location, (tuple, list)):
        return "root", None
    for part in location[:12]:
        if isinstance(part, int) and 0 <= part <= 9999:
            if parts:
                parts[-1] += f"[{part}]"
            else:
                parts.append(f"[{part}]")
        elif isinstance(part, str) and _SAFE_SCHEMA_PATH_PART.fullmatch(part):
            parts.append(part)
            field_name = part
        else:
            return "unknown", None
    return ".".join(parts)[:256] or "root", field_name


def _safe_received_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def schema_error_diagnostic(exc: Exception, *, attempt: str) -> dict[str, Any]:
    """Normalize one schema failure without retaining raw output or prose."""
    diagnostic = {
        "diagnostic_rule": "structured_output_schema", "parse_stage": "output_parse",
        "error_class": "PydanticValidationError" if isinstance(exc, ValidationError) else type(exc).__name__,
        "schema_error_type": "unknown_schema_error", "path": "root",
        "expected": None, "received_type": None, "missing_field": None,
        "unexpected_field": None, "invalid_enum_value": None,
        "attempt": attempt, "earlier_attempt_failed": False,
        "safe_reason": "The structured output did not match the required schema.",
    }
    if not isinstance(exc, ValidationError):
        return diagnostic
    errors = exc.errors(include_url=False, include_context=False, include_input=True)
    if not errors:
        return diagnostic
    error = errors[0]
    raw_type = error.get("type")
    path, field_name = _safe_schema_path(error.get("loc"))
    diagnostic["path"] = path
    diagnostic["received_type"] = _safe_received_type(error.get("input"))
    if raw_type == "json_invalid":
        diagnostic.update(
            schema_error_type="invalid_json", received_type=None,
            safe_reason="The model output was not valid JSON.",
        )
    elif raw_type == "missing":
        diagnostic.update(
            schema_error_type="missing_required_field", expected="required field",
            received_type=None, missing_field=field_name,
            safe_reason="A required schema field was missing.",
        )
    elif raw_type == "extra_forbidden":
        diagnostic.update(
            schema_error_type="unexpected_field", unexpected_field=field_name,
            safe_reason="The output contained an unexpected schema field.",
        )
    elif raw_type == "literal_error":
        invalid = error.get("input")
        allowed = _CONTROLLED_ENUM_VALUES.get(field_name or "", frozenset())
        diagnostic.update(
            schema_error_type="invalid_enum",
            expected="one of: " + ", ".join(sorted(allowed)) if allowed else "controlled enum value",
            invalid_enum_value=(
                invalid if isinstance(invalid, str) and _SAFE_SCHEMA_TOKEN.fullmatch(invalid) else None
            ),
            safe_reason="A controlled enum field contained an invalid value.",
        )
    elif raw_type in {"model_type", "dict_type", "mapping_type"}:
        diagnostic.update(
            schema_error_type="invalid_object_shape", expected="object",
            safe_reason="A schema object had the wrong shape.",
        )
    elif raw_type in {"list_type", "tuple_type", "set_type"}:
        diagnostic.update(
            schema_error_type="invalid_array_shape", expected="array",
            safe_reason="A schema array had the wrong shape.",
        )
    elif raw_type in {
        "string_type", "int_type", "float_type", "bool_type", "none_required",
    }:
        expected = {
            "string_type": "string", "int_type": "integer", "float_type": "number",
            "bool_type": "boolean", "none_required": "null",
        }[raw_type]
        diagnostic.update(
            schema_error_type="wrong_type", expected=expected,
            safe_reason="A schema field had the wrong type.",
        )
    return diagnostic


def headline_key_diagnostic(exc: GUTSValidationError, *, attempt: str) -> dict[str, Any] | None:
    if exc.validator_rule != "headline_key":
        return None
    received = exc.statement_key
    safe_received = (
        received
        if isinstance(received, str) and _SAFE_SCHEMA_TOKEN.fullmatch(received)
        else None
    )
    return {
        "diagnostic_rule": "headline_key", "parse_stage": "output_validation",
        "error_class": "GUTSValidationError", "schema_error_type": "invalid_enum",
        "path": "headline.statement_key", "expected": "headline",
        "received_type": "string" if isinstance(received, str) else "unknown",
        "missing_field": None, "unexpected_field": None,
        "invalid_enum_value": safe_received, "received_key": safe_received,
        "required_key": "headline", "attempt": attempt,
        "earlier_attempt_failed": False,
        "safe_reason": "The headline used an invalid reserved statement key.",
    }


@dataclass(frozen=True)
class ProviderErrorDiagnostic:
    provider: str
    model: str
    subtype: str
    http_status: int | None
    provider_code: str | None
    provider_type: str | None
    parameter: str | None
    request_id: str | None
    retryable: bool
    safe_explanation: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "subtype": self.subtype,
            "http_status": self.http_status,
            "provider_code": self.provider_code,
            "provider_type": self.provider_type,
            "parameter": self.parameter,
            "request_id": self.request_id,
            "retryable": self.retryable,
            "safe_explanation": self.safe_explanation,
        }


_PROVIDER_EXPLANATIONS = {
    "model_not_found": "The configured model was not found by the provider.",
    "model_access_denied": "The provider account does not have access to the configured model.",
    "unsupported_parameter": "The configured model does not support a request parameter.",
    "invalid_request": "The provider rejected the request configuration.",
    "invalid_output_schema": "The provider rejected the structured-output schema.",
    "rate_limited": "The provider temporarily rate limited the request.",
    "quota_exceeded": "The provider account quota is unavailable or exhausted.",
    "authentication_failed": "The provider rejected authentication.",
    "provider_unavailable": "The provider was unavailable or could not be reached.",
    "provider_timeout": "The provider request timed out.",
    "unknown_provider_error": "The provider returned an unclassified safe error.",
}


def sanitize_openai_provider_error(
    exc: Exception, *, model: str, provider: str = "openai",
) -> ProviderErrorDiagnostic:
    """Extract only allowlisted structured metadata from an OpenAI SDK error."""
    class_name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    status = status if isinstance(status, int) and 100 <= status <= 599 else None
    raw_code = getattr(exc, "code", None)
    code = raw_code if isinstance(raw_code, str) and raw_code in _SAFE_PROVIDER_CODES else None
    raw_type = getattr(exc, "type", None)
    provider_type = raw_type if isinstance(raw_type, str) and raw_type in _SAFE_PROVIDER_TYPES else None
    raw_param = getattr(exc, "param", None)
    parameter = raw_param if isinstance(raw_param, str) and raw_param in _SAFE_PROVIDER_PARAMS else None
    raw_request_id = getattr(exc, "request_id", None)
    request_id = (
        raw_request_id
        if isinstance(raw_request_id, str) and _SAFE_REQUEST_ID.fullmatch(raw_request_id)
        else None
    )

    if class_name == "APITimeoutError":
        subtype = "provider_timeout"
    elif class_name == "AuthenticationError" or status == 401 or code == "invalid_api_key":
        subtype = "authentication_failed"
    elif code in {"insufficient_quota", "quota_exceeded", "billing_hard_limit_reached"}:
        subtype = "quota_exceeded"
    elif class_name == "RateLimitError" or status == 429:
        subtype = "rate_limited"
    elif code in {"model_not_found", "model_not_available"} or class_name == "NotFoundError":
        subtype = "model_not_found"
    elif code == "model_access_denied" or class_name == "PermissionDeniedError" or status == 403:
        subtype = "model_access_denied"
    elif code in {"invalid_json_schema", "invalid_schema", "schema_validation_error"} or parameter in {
        "text.format", "text.format.schema", "response_format", "response_format.json_schema",
    }:
        subtype = "invalid_output_schema"
    elif code in {"unsupported_parameter", "unsupported_value"} or (
        code == "invalid_parameter" and parameter is not None
    ):
        subtype = "unsupported_parameter"
    elif class_name in {"APIConnectionError", "InternalServerError"} or (status is not None and status >= 500):
        subtype = "provider_unavailable"
    elif class_name in {"BadRequestError", "UnprocessableEntityError"} or status in {400, 422}:
        subtype = "invalid_request"
    else:
        subtype = "unknown_provider_error"

    retryable = subtype in {"rate_limited", "provider_unavailable", "provider_timeout", "unknown_provider_error"}
    return ProviderErrorDiagnostic(
        provider=provider,
        model=model,
        subtype=subtype,
        http_status=status,
        provider_code=code,
        provider_type=provider_type,
        parameter=parameter,
        request_id=request_id,
        retryable=retryable,
        safe_explanation=_PROVIDER_EXPLANATIONS[subtype],
    )


class GUTSModelError(RuntimeError):
    def __init__(
        self, safe_category: str, safe_message: str, *, retryable: bool,
        usage: dict[str, int | None] | None = None, model_ms: float = 0.0,
        stage: str = "model_call",
        validation_debug: dict[str, Any] | None = None,
        provider_debug: dict[str, Any] | None = None,
        schema_debug: dict[str, Any] | None = None,
    ):
        super().__init__(safe_message)
        self.safe_category = safe_category
        self.safe_message = safe_message
        self.retryable = retryable
        self.usage = usage or {}
        self.model_ms = model_ms
        self.stage = stage
        self.validation_debug = validation_debug
        self.provider_debug = provider_debug
        self.schema_debug = schema_debug


def _validation_debug(exc: GUTSValidationError) -> dict[str, Any] | None:
    if not exc.rejected_statement_text or not exc.statement_key or not exc.validator_rule:
        return None
    placement = exc.statement_placement or "unknown"
    section_type = placement.removeprefix("section:") if placement.startswith("section:") else None
    controlled_placement = "section" if section_type else placement
    return {
        "statement_key": exc.statement_key,
        "placement": controlled_placement,
        "section_type": section_type,
        "confidence": exc.statement_confidence or "unknown",
        "cited_source_ids": tuple(exc.cited_source_ids),
        "cited_source_kinds": tuple(exc.cited_source_kinds),
        "allowed_source_classes": tuple(exc.allowed_source_classes),
        "required_source_classes": tuple(exc.required_source_classes),
        "grounded_field": exc.grounded_field,
        "required_source_id": exc.required_source_id,
        "required_source_ids": tuple(exc.required_source_ids),
        "statement_text": exc.rejected_statement_text,
        "validator_rule": exc.validator_rule,
        "validator_reason": exc.safe_message,
    }


def _log_output_validation_failure(
    output: ModelBriefingOutput, manifest: GUTSManifest, exc: GUTSValidationError,
) -> None:
    """Emit one bounded JSON diagnostic through the production message formatter."""
    def bounded_text(value: str | None, maximum: int) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized if len(normalized) <= maximum else normalized[: maximum - 1].rstrip() + "…"

    def sanitized_actor(actor: Any) -> dict[str, Any]:
        return {
            "user_id": actor.user_id,
            "display_name": bounded_text(actor.display_name, 200),
            "email": "[redacted]" if actor.email else None,
        }

    def sanitized_author(author: Any) -> dict[str, Any] | None:
        if author is None:
            return None
        return {
            "user_id": author.user_id,
            "display_name": bounded_text(author.display_name, 200),
            "address": "[redacted]" if author.address else None,
        }

    statements = [
        ("headline", output.headline),
        *(("summary", statement) for statement in output.summary_statements),
        *(
            (f"section:{section.section_type}", statement)
            for section in output.sections for statement in section.statements
        ),
    ]
    match = next(
        (
            (index, placement, statement)
            for index, (placement, statement) in enumerate(statements)
            if statement.statement_key == exc.statement_key
        ),
        None,
    )
    index, placement, statement = match or (None, exc.statement_placement, None)
    cited_source_ids = tuple(statement.source_ids) if statement is not None else tuple(exc.cited_source_ids)
    cited = set(cited_source_ids)
    organizational_evidence = [
        {
            "source_id": source.source_id,
            "source_type": source.source_type,
            "author": sanitized_author(source.author),
            "text": bounded_text(source.text, 500),
        }
        for source in manifest.evidence.sources
        if source.source_id in cited and source.source_class == "organizational_knowledge"
    ][:10]
    attribution = statement.attribution if statement is not None else None
    payload = {
        "event": "guts_output_validation_failed",
        "statement_index": index,
        "statement_placement": placement,
        "statement_key": bounded_text(
            statement.statement_key if statement is not None else exc.statement_key, 200,
        ),
        "rejected_text": bounded_text(
            statement.text if statement is not None else exc.rejected_statement_text, 1000,
        ),
        "attribution": (
            {
                "type": attribution.type,
                "actors": [sanitized_actor(actor) for actor in attribution.actors[:10]],
            }
            if attribution is not None else None
        ),
        "cited_source_ids": [bounded_text(source_id, 500) for source_id in cited_source_ids[:20]],
        "resolved_organizational_evidence": organizational_evidence,
        "validation_rule": bounded_text(exc.validator_rule, 100),
    }
    # Railway receives stdout/stderr after Uvicorn's default formatter, which
    # retains only LogRecord.message. Serialize the complete diagnostic there.
    logger.error(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True))


@dataclass(frozen=True)
class GUTSModelCallResult:
    output: ModelBriefingOutput
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    model_ms: float


@dataclass(frozen=True)
class GUTSValidatedGenerationResult:
    output: ValidatedBriefingOutput
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    model_ms: float
    validation_retry_count: int
    first_attempt_validation_category: str | None = None


@dataclass(frozen=True)
class GUTSModelProbeResult:
    success: bool
    provider: str
    model: str
    diagnostic: ProviderErrorDiagnostic | None = None


class GUTSModelClientProtocol(Protocol):
    def generate(self, manifest: GUTSManifest) -> GUTSModelCallResult: ...
    def retry_with_validation_feedback(self, manifest: GUTSManifest, feedback: str) -> GUTSModelCallResult: ...


def _usage(response: Any) -> dict[str, int | None]:
    usage = getattr(response, "usage", None)
    return {
        key: getattr(usage, key, None) if usage else None
        for key in ("input_tokens", "output_tokens", "total_tokens")
    }


def _safe_feedback(value: str) -> str:
    if value in SAFE_RETRY_FEEDBACK:
        return value
    if value.startswith(_CITATION_FEEDBACK_PREFIX) and len(value) <= _MAX_CITATION_FEEDBACK_CHARACTERS:
        try:
            payload = json.loads(value.removeprefix(_CITATION_FEEDBACK_PREFIX))
        except (json.JSONDecodeError, TypeError):
            payload = None
        if (
            isinstance(payload, dict)
            and set(payload) == {"instruction", "invalid_source_ids", "allowed_source_ids"}
            and payload.get("instruction") == "Copy only exact allowed_source_ids values. Field-name keys, citation labels, conflict IDs, hashes, record IDs, and metadata values are not citations."
            and all(isinstance(items, list) for items in (payload.get("invalid_source_ids"), payload.get("allowed_source_ids")))
            and all(
                isinstance(item, str) and 0 < len(item) <= 256 and not any(ord(char) < 32 for char in item)
                for key in ("invalid_source_ids", "allowed_source_ids") for item in payload[key]
            )
        ):
            return value
    if value.startswith(_GROUNDED_FIELD_FEEDBACK_PREFIX) and len(value) <= _MAX_CITATION_FEEDBACK_CHARACTERS:
        try:
            payload = json.loads(value.removeprefix(_GROUNDED_FIELD_FEEDBACK_PREFIX))
        except (json.JSONDecodeError, TypeError):
            payload = None
        if (
            isinstance(payload, dict)
            and set(payload) == {
                "instruction", "statement_key", "placement", "field_name",
                "required_source_id", "cited_source_ids",
            }
            and payload.get("instruction") == "Cite the exact required current-state source ID; split the statement if it contains another independently supported claim."
            and all(isinstance(payload.get(key), str) and 0 < len(payload[key]) <= 256 for key in (
                "statement_key", "placement", "field_name", "required_source_id",
            ))
            and isinstance(payload.get("cited_source_ids"), list)
            and all(isinstance(item, str) and 0 < len(item) <= 256 for item in payload["cited_source_ids"])
        ):
            return value
    if value.startswith(_ATTRIBUTION_FEEDBACK_PREFIX) and len(value) <= _MAX_CITATION_FEEDBACK_CHARACTERS:
        try:
            payload = json.loads(value.removeprefix(_ATTRIBUTION_FEEDBACK_PREFIX))
        except (json.JSONDecodeError, TypeError):
            payload = None
        if (
            isinstance(payload, dict)
            and set(payload) == {
                "instruction", "statement_key", "placement", "confidence",
                "cited_source_kinds", "safe_example", "multiple_actors",
            }
            and payload.get("instruction") in {
                "Name the person who made the recommendation, plan, concern, or observation; do not generalize one person's statement into organizational consensus; use concise actor-attributed wording.",
                "Multiple communication actors contributed to this statement; split distinct ideas into separate statements, preserve each actor's attribution, and avoid anonymous passive constructions.",
                "Copy attribution only from source_attribution attached to the cited source IDs; do not substitute another actor with the same display name.",
            }
            and payload.get("safe_example") == "A named person recommended the approach."
            and isinstance(payload.get("multiple_actors"), bool)
            and all(isinstance(payload.get(key), str) and 0 < len(payload[key]) <= 256 for key in (
                "statement_key", "placement", "confidence",
            ))
            and isinstance(payload.get("cited_source_kinds"), list)
            and all(isinstance(item, str) and 0 < len(item) <= 128 for item in payload["cited_source_kinds"])
        ):
            return value
    return "Correct the response to match the strict schema, citation, and safety rules."


def _validation_feedback(exc: GUTSValidationError) -> str:
    if exc.invalid_source_ids and exc.allowed_source_ids:
        return _CITATION_FEEDBACK_PREFIX + json.dumps({
            "instruction": "Copy only exact allowed_source_ids values. Field-name keys, citation labels, conflict IDs, hashes, record IDs, and metadata values are not citations.",
            "invalid_source_ids": list(exc.invalid_source_ids),
            "allowed_source_ids": list(exc.allowed_source_ids),
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if exc.statement_key and exc.required_source_id and exc.grounded_field:
        return _GROUNDED_FIELD_FEEDBACK_PREFIX + json.dumps({
            "instruction": "Cite the exact required current-state source ID; split the statement if it contains another independently supported claim.",
            "statement_key": exc.statement_key,
            "placement": exc.statement_placement or "unknown",
            "field_name": exc.grounded_field,
            "required_source_id": exc.required_source_id,
            "cited_source_ids": list(exc.cited_source_ids),
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if exc.statement_key and exc.statement_confidence and exc.cited_source_kinds:
        instruction = (
            "Copy attribution only from source_attribution attached to the cited source IDs; do not substitute another actor with the same display name."
            if exc.validator_rule == "actor_source_mismatch" else
            "Multiple communication actors contributed to this statement; split distinct ideas into separate statements, preserve each actor's attribution, and avoid anonymous passive constructions."
            if exc.multiple_cited_actors else
            "Name the person who made the recommendation, plan, concern, or observation; do not generalize one person's statement into organizational consensus; use concise actor-attributed wording."
        )
        return _ATTRIBUTION_FEEDBACK_PREFIX + json.dumps({
            "instruction": instruction,
            "statement_key": exc.statement_key,
            "placement": exc.statement_placement or "unknown",
            "confidence": exc.statement_confidence,
            "cited_source_kinds": list(exc.cited_source_kinds),
            "safe_example": "A named person recommended the approach.",
            "multiple_actors": exc.multiple_cited_actors,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return exc.feedback


class GUTSModelClient:
    def __init__(self, *, client=None, model: str | None = None):
        self.provider = config.GUTS_AI_PROVIDER
        self.model = model or config.GUTS_AI_MODEL
        try:
            self.prompt = resolve_prompt(config.GUTS_PROMPT_VERSION)
            if self.prompt.output_schema_version != config.GUTS_OUTPUT_SCHEMA_VERSION:
                raise GUTSPromptConfigurationError(
                    GUTSPromptConfigurationError.safe_message,
                )
        except GUTSPromptConfigurationError as exc:
            raise GUTSModelError(
                exc.safe_category, exc.safe_message, retryable=False,
                stage="configuration",
            ) from None
        if self.provider != "openai" or not self.model or (client is None and not config.OPENAI_API_KEY):
            raise GUTSModelError(
                "model_configuration_missing", "GUTS model generation is not configured.", retryable=False,
            )
        if client is None:
            from openai import OpenAI
            client = OpenAI(
                api_key=config.OPENAI_API_KEY,
                timeout=config.GUTS_TIMEOUT_SECONDS,
                max_retries=config.GUTS_MAX_RETRIES,
            )
        self.client = client

    def generate(self, manifest: GUTSManifest) -> GUTSModelCallResult:
        return self._call(manifest, validation_feedback=None)

    def retry_with_validation_feedback(self, manifest: GUTSManifest, feedback: str) -> GUTSModelCallResult:
        return self._call(manifest, validation_feedback=_safe_feedback(feedback))

    def _call(self, manifest: GUTSManifest, *, validation_feedback: str | None) -> GUTSModelCallResult:
        started = perf_counter()
        response = None
        try:
            request = build_responses_request(
                self.model,
                instructions=self.prompt.instructions,
                input=manifest_input(
                    manifest, prompt=self.prompt, validation_feedback=validation_feedback,
                ),
                text={"format": {
                    "type": "json_schema", "name": "guts_briefing",
                    "strict": True, "schema": guts_output_schema(
                        allowed_source_ids=manifest.allowed_source_ids(),
                        output_schema_version=self.prompt.output_schema_version,
                        allowed_actors=_allowed_actor_values(manifest),
                    ),
                }},
                max_output_tokens=config.GUTS_MAX_OUTPUT_TOKENS,
                metadata={
                    "prompt_version": self.prompt.version,
                    "manifest_version": manifest.manifest_version,
                    "output_schema_version": self.prompt.output_schema_version,
                },
            )
            response = self.client.responses.create(**request)
            model_ms = round((perf_counter() - started) * 1000, 2)
            usage = _usage(response)
            raw = getattr(response, "output_text", None)
            if not isinstance(raw, str) or not raw.strip():
                logger.warning(
                    "guts_model_call_failed provider=%s model=%s category=model_schema_invalid model_ms=%s retry=%s",
                    self.provider, self.model, model_ms, str(validation_feedback is not None).lower(),
                )
                raise GUTSModelError(
                    "model_schema_invalid", "The model returned no structured briefing.", retryable=True,
                    usage=usage, model_ms=model_ms, stage="output_parse",
                    schema_debug={
                        "diagnostic_rule": "structured_output_schema",
                        "parse_stage": "output_parse", "error_class": "EmptyStructuredOutput",
                        "schema_error_type": "unknown_schema_error", "path": "root",
                        "expected": "structured JSON object", "received_type": "empty",
                        "missing_field": None, "unexpected_field": None,
                        "invalid_enum_value": None,
                        "attempt": "corrective_retry" if validation_feedback is not None else "initial",
                        "earlier_attempt_failed": False,
                        "safe_reason": "The model returned no structured output.",
                    },
                )
            try:
                if self.prompt.output_schema_version == "guts-output-v2":
                    provider_output = ProviderBriefingOutputV2.model_validate_json(raw)
                    output = assign_v2_statement_keys(provider_output)
                else:
                    output = ModelBriefingOutput.model_validate_json(raw)
            except (ValidationError, ValueError, TypeError) as exc:
                logger.warning(
                    "guts_model_call_failed provider=%s model=%s category=model_schema_invalid model_ms=%s retry=%s",
                    self.provider, self.model, model_ms, str(validation_feedback is not None).lower(),
                )
                raise GUTSModelError(
                    "model_schema_invalid", "The model returned an invalid structured briefing.", retryable=True,
                    usage=usage, model_ms=model_ms, stage="output_parse",
                    schema_debug=schema_error_diagnostic(
                        exc, attempt="corrective_retry" if validation_feedback is not None else "initial",
                    ),
                ) from exc
        except GUTSModelError:
            raise
        except Exception as exc:
            model_ms = round((perf_counter() - started) * 1000, 2)
            diagnostic = sanitize_openai_provider_error(exc, model=self.model, provider=self.provider)
            if diagnostic.subtype == "provider_timeout":
                category, message = "model_timeout", "The GUTS model request timed out."
            elif diagnostic.subtype in {"rate_limited", "quota_exceeded"}:
                category, message = "model_provider_error", "The GUTS model provider is temporarily rate limited."
            else:
                category, message = "model_provider_error", "The GUTS model provider could not complete the request."
            logger.warning(
                "guts_model_call_failed provider=%s model=%s category=%s provider_error_subtype=%s "
                "http_status=%s provider_code=%s provider_type=%s provider_parameter=%s "
                "provider_request_id=%s retryable=%s model_ms=%s retry=%s",
                self.provider, self.model, category, diagnostic.subtype, diagnostic.http_status,
                diagnostic.provider_code, diagnostic.provider_type, diagnostic.parameter,
                diagnostic.request_id, str(diagnostic.retryable).lower(), model_ms,
                str(validation_feedback is not None).lower(),
            )
            raise GUTSModelError(
                category, message, retryable=diagnostic.retryable, model_ms=model_ms,
                provider_debug=diagnostic.as_dict(),
            ) from exc
        logger.info(
            "guts_model_call_complete provider=%s model=%s model_ms=%s input_tokens=%s output_tokens=%s retry=%s",
            self.provider, self.model, model_ms, usage["input_tokens"], usage["output_tokens"],
            str(validation_feedback is not None).lower(),
        )
        return GUTSModelCallResult(
            output=output, provider=self.provider, model=self.model,
            input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
            total_tokens=usage["total_tokens"], model_ms=model_ms,
        )


def _sum_optional(first: int | None, second: int | None) -> int | None:
    if first is None and second is None:
        return None
    return int(first or 0) + int(second or 0)


def generate_validated_briefing(
    manifest: GUTSManifest, *, client: GUTSModelClientProtocol | None = None,
    validator: GUTSOutputValidator | None = None,
) -> GUTSValidatedGenerationResult:
    model_client = client or GUTSModelClient()
    output_validator = validator or GUTSOutputValidator(
        output_schema_version=model_client.prompt.output_schema_version
        if isinstance(model_client, GUTSModelClient) else "guts-output-v1",
    )
    first_call: GUTSModelCallResult | None = None
    first_error: GUTSModelError | GUTSValidationError | None = None
    try:
        first_call = model_client.generate(manifest)
        validated = output_validator.validate(first_call.output, manifest)
        return GUTSValidatedGenerationResult(
            output=validated, provider=first_call.provider, model=first_call.model,
            input_tokens=first_call.input_tokens, output_tokens=first_call.output_tokens,
            total_tokens=first_call.total_tokens, model_ms=first_call.model_ms,
            validation_retry_count=0,
        )
    except GUTSValidationError as exc:
        first_error = exc
        feedback = _validation_feedback(exc)
    except GUTSModelError as exc:
        if not exc.retryable or exc.safe_category not in {
            "model_schema_invalid", "model_citation_invalid", "model_output_unsafe",
        }:
            raise
        first_error = exc
        feedback = "Correct the response to match the strict schema, citation, and safety rules."
    try:
        second_call = model_client.retry_with_validation_feedback(manifest, feedback)
        validated = output_validator.validate(second_call.output, manifest)
    except GUTSValidationError as exc:
        schema_debug = headline_key_diagnostic(exc, attempt="corrective_retry")
        if schema_debug:
            schema_debug["earlier_attempt_failed"] = first_error is not None
        _log_output_validation_failure(second_call.output, manifest, exc)
        raise GUTSModelError(
            exc.safe_category, exc.safe_message, retryable=False, stage=exc.stage,
            validation_debug=_validation_debug(exc),
            schema_debug=schema_debug,
        ) from exc
    except GUTSModelError as exc:
        if exc.safe_category in {"model_schema_invalid", "model_citation_invalid", "model_output_unsafe"}:
            schema_debug = dict(exc.schema_debug) if exc.schema_debug else None
            if schema_debug and isinstance(first_error, GUTSModelError):
                schema_debug["earlier_attempt_failed"] = (
                    first_error.safe_category == "model_schema_invalid"
                )
            raise GUTSModelError(
                exc.safe_category, exc.safe_message, retryable=False, usage=exc.usage,
                model_ms=exc.model_ms, stage=exc.stage,
                validation_debug=exc.validation_debug,
                schema_debug=schema_debug,
            ) from exc
        raise
    first_usage = first_call
    first_error_usage = first_error if isinstance(first_error, GUTSModelError) else None
    return GUTSValidatedGenerationResult(
        output=validated, provider=second_call.provider, model=second_call.model,
        input_tokens=_sum_optional(
            first_usage.input_tokens if first_usage else (first_error_usage.usage.get("input_tokens") if first_error_usage else None),
            second_call.input_tokens,
        ),
        output_tokens=_sum_optional(
            first_usage.output_tokens if first_usage else (first_error_usage.usage.get("output_tokens") if first_error_usage else None),
            second_call.output_tokens,
        ),
        total_tokens=_sum_optional(
            first_usage.total_tokens if first_usage else (first_error_usage.usage.get("total_tokens") if first_error_usage else None),
            second_call.total_tokens,
        ),
        model_ms=(first_usage.model_ms if first_usage else (first_error_usage.model_ms if first_error_usage else 0.0)) + second_call.model_ms,
        validation_retry_count=1,
        first_attempt_validation_category=first_error.safe_category,
    )


def probe_guts_model(*, client=None, model: str | None = None) -> GUTSModelProbeResult:
    """Make one evidence-free structured request for development compatibility checks."""
    resolved_model = model or config.GUTS_AI_MODEL
    provider = config.GUTS_AI_PROVIDER
    if provider != "openai" or not resolved_model or (client is None and not config.OPENAI_API_KEY):
        raise GUTSModelError(
            "model_configuration_missing", "GUTS model generation is not configured.", retryable=False,
        )
    if client is None:
        from openai import OpenAI
        client = OpenAI(
            api_key=config.OPENAI_API_KEY,
            timeout=config.GUTS_TIMEOUT_SECONDS,
            max_retries=config.GUTS_MAX_RETRIES,
        )
    try:
        request = build_responses_request(
            resolved_model,
            input="Return a JSON object with ok set to true.",
            text={"format": {
                "type": "json_schema", "name": "guts_model_probe", "strict": True,
                "schema": {
                    **model_probe_output_schema(),
                },
            }},
            max_output_tokens=32,
        )
        client.responses.create(**request)
    except Exception as exc:
        return GUTSModelProbeResult(
            success=False, provider=provider, model=resolved_model,
            diagnostic=sanitize_openai_provider_error(exc, model=resolved_model, provider=provider),
        )
    return GUTSModelProbeResult(success=True, provider=provider, model=resolved_model)
