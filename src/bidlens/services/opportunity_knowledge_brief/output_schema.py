"""Exact strict provider schema for GUTS model output."""

from __future__ import annotations

from typing import Any
from copy import deepcopy

from ... import config


SECTION_TYPES = [
    "current_state", "official_updates", "organizational_knowledge",
    "important_history", "uncertainties",
]


def _statement_schema(*, allowed_source_ids: tuple[str, ...] | None = None) -> dict[str, Any]:
    source_id_schema: dict[str, Any] = {"type": "string"}
    if allowed_source_ids is not None:
        source_id_schema["enum"] = list(allowed_source_ids)
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            # OpenAI strict Structured Outputs supports only a subset of JSON
            # Schema string constraints. Non-empty and length checks therefore
            # remain deterministic application validation concerns.
            "statement_key": {"type": "string"},
            "text": {"type": "string"},
            "importance": {"type": "string", "enum": ["high", "normal"]},
            "confidence": {"type": "string", "enum": ["supported", "attributed", "uncertain"]},
            "source_ids": {
                "type": "array", "items": source_id_schema,
                "minItems": 1, "maxItems": 20,
            },
        },
        "required": ["statement_key", "text", "importance", "confidence", "source_ids"],
    }


def guts_output_schema(*, allowed_source_ids: tuple[str, ...] | None = None) -> dict[str, Any]:
    statement = _statement_schema(allowed_source_ids=allowed_source_ids)
    headline = deepcopy(statement)
    headline["properties"]["importance"] = {"type": "string", "enum": ["high"]}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "headline": headline,
            "summary_statements": {
                "type": "array", "items": statement, "minItems": 1,
                "maxItems": config.GUTS_MAX_SUMMARY_STATEMENTS,
            },
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "section_type": {"type": "string", "enum": SECTION_TYPES},
                        "statements": {
                            "type": "array", "items": statement, "minItems": 1,
                            "maxItems": config.GUTS_MAX_STATEMENTS_PER_SECTION,
                        },
                    },
                    "required": ["section_type", "statements"],
                },
                "maxItems": config.GUTS_MAX_SECTIONS,
            },
        },
        "required": ["headline", "summary_statements", "sections"],
    }
