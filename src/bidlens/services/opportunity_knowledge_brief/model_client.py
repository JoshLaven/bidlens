"""Dedicated OpenAI model boundary and one-retry validation flow for GUTS."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from time import perf_counter
from typing import Any, Protocol

from pydantic import ValidationError

from ... import config
from .contracts import GUTSManifest, ModelBriefingOutput, ValidatedBriefingOutput
from .output_schema import guts_output_schema
from .output_validation import GUTSOutputValidator, GUTSValidationError
from .prompt import PROMPT_VERSION, SYSTEM_INSTRUCTIONS, manifest_input


logger = logging.getLogger(__name__)
_CITATION_FEEDBACK_PREFIX = "citation_inventory_v1:"
_MAX_CITATION_FEEDBACK_CHARACTERS = 16_000
SAFE_RETRY_FEEDBACK = frozenset({
    "Use statement_key 'headline' for the headline.",
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
    "Use only source IDs present in the manifest.",
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


class GUTSModelError(RuntimeError):
    def __init__(
        self, safe_category: str, safe_message: str, *, retryable: bool,
        usage: dict[str, int | None] | None = None, model_ms: float = 0.0,
        stage: str = "model_call",
    ):
        super().__init__(safe_message)
        self.safe_category = safe_category
        self.safe_message = safe_message
        self.retryable = retryable
        self.usage = usage or {}
        self.model_ms = model_ms
        self.stage = stage


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
            and payload.get("instruction") == "Use only exact IDs from allowed_source_ids; do not shorten IDs, omit prefixes, or use citation labels."
            and all(isinstance(items, list) for items in (payload.get("invalid_source_ids"), payload.get("allowed_source_ids")))
            and all(
                isinstance(item, str) and 0 < len(item) <= 256 and not any(ord(char) < 32 for char in item)
                for key in ("invalid_source_ids", "allowed_source_ids") for item in payload[key]
            )
        ):
            return value
    return "Correct the response to match the strict schema, citation, and safety rules."


def _validation_feedback(exc: GUTSValidationError) -> str:
    if exc.invalid_source_ids and exc.allowed_source_ids:
        return _CITATION_FEEDBACK_PREFIX + json.dumps({
            "instruction": "Use only exact IDs from allowed_source_ids; do not shorten IDs, omit prefixes, or use citation labels.",
            "invalid_source_ids": list(exc.invalid_source_ids),
            "allowed_source_ids": list(exc.allowed_source_ids),
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return exc.feedback


class GUTSModelClient:
    def __init__(self, *, client=None, model: str | None = None):
        self.provider = config.GUTS_AI_PROVIDER
        self.model = model or config.GUTS_AI_MODEL
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
            response = self.client.responses.create(
                model=self.model,
                instructions=SYSTEM_INSTRUCTIONS,
                input=manifest_input(manifest, validation_feedback=validation_feedback),
                text={"format": {
                    "type": "json_schema", "name": "guts_briefing",
                    "strict": True, "schema": guts_output_schema(),
                }},
                max_output_tokens=config.GUTS_MAX_OUTPUT_TOKENS,
                temperature=0,
                metadata={
                    "prompt_version": PROMPT_VERSION,
                    "manifest_version": manifest.manifest_version,
                    "output_schema_version": config.GUTS_OUTPUT_SCHEMA_VERSION,
                },
            )
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
                )
            try:
                output = ModelBriefingOutput.model_validate_json(raw)
            except (ValidationError, ValueError, TypeError) as exc:
                logger.warning(
                    "guts_model_call_failed provider=%s model=%s category=model_schema_invalid model_ms=%s retry=%s",
                    self.provider, self.model, model_ms, str(validation_feedback is not None).lower(),
                )
                raise GUTSModelError(
                    "model_schema_invalid", "The model returned an invalid structured briefing.", retryable=True,
                    usage=usage, model_ms=model_ms, stage="output_parse",
                ) from exc
        except GUTSModelError:
            raise
        except Exception as exc:
            model_ms = round((perf_counter() - started) * 1000, 2)
            name = type(exc).__name__.casefold()
            if "timeout" in name:
                category, message, retryable = "model_timeout", "The GUTS model request timed out.", True
            elif "ratelimit" in name or "rate_limit" in name:
                category, message, retryable = "model_provider_error", "The GUTS model provider is temporarily rate limited.", True
            else:
                category, message, retryable = "model_provider_error", "The GUTS model provider could not complete the request.", True
            logger.warning(
                "guts_model_call_failed provider=%s model=%s category=%s model_ms=%s retry=%s",
                self.provider, self.model, category, model_ms, str(validation_feedback is not None).lower(),
            )
            raise GUTSModelError(category, message, retryable=retryable, model_ms=model_ms) from exc
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
    output_validator = validator or GUTSOutputValidator()
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
        raise GUTSModelError(exc.safe_category, exc.safe_message, retryable=False, stage=exc.stage) from exc
    except GUTSModelError as exc:
        if exc.safe_category in {"model_schema_invalid", "model_citation_invalid", "model_output_unsafe"}:
            raise GUTSModelError(
                exc.safe_category, exc.safe_message, retryable=False, usage=exc.usage,
                model_ms=exc.model_ms, stage=exc.stage,
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
