from __future__ import annotations

import pytest
from pydantic import ValidationError

from bidlens.services.opportunity_knowledge_brief.contracts import ModelBriefingOutput
from bidlens.services.opportunity_knowledge_brief.model_client import schema_error_diagnostic


PRIVATE = "PRIVATE MODEL PROSE source body prompt manifest sk-secret"


def _diagnostic(raw: str):
    try:
        ModelBriefingOutput.model_validate_json(raw)
    except ValidationError as exc:
        return schema_error_diagnostic(exc, attempt="corrective_retry")
    raise AssertionError("fixture unexpectedly passed")


@pytest.mark.parametrize(("raw", "category", "path", "field"), [
    ('{"summary_statements":[],"sections":[]}', "missing_required_field", "headline", "headline"),
    (
        '{"headline":{"statement_key":"headline","text":"x","importance":"high",'
        '"confidence":"supported","source_ids":["a"],"surprise":"x"},'
        '"summary_statements":[],"sections":[]}',
        "unexpected_field", "headline.surprise", "surprise",
    ),
])
def test_missing_and_unexpected_fields_are_safely_normalized(raw, category, path, field):
    diagnostic = _diagnostic(raw)
    assert diagnostic["schema_error_type"] == category
    assert diagnostic["path"] == path
    key = "missing_field" if category == "missing_required_field" else "unexpected_field"
    assert diagnostic[key] == field
    assert PRIVATE not in str(diagnostic)


def test_wrong_object_type_reports_expected_and_received_types():
    diagnostic = _diagnostic('{"headline":[],"summary_statements":[],"sections":[]}')
    assert diagnostic["schema_error_type"] == "invalid_object_shape"
    assert diagnostic["expected"] == "object"
    assert diagnostic["received_type"] == "array"


def test_invalid_enum_exposes_only_short_controlled_token():
    diagnostic = _diagnostic(
        '{"headline":{"statement_key":"headline","text":"x","importance":"urgent",'
        '"confidence":"supported","source_ids":["a"]},"summary_statements":[],"sections":[]}'
    )
    assert diagnostic["schema_error_type"] == "invalid_enum"
    assert diagnostic["path"] == "headline.importance"
    assert diagnostic["invalid_enum_value"] == "urgent"
    assert diagnostic["expected"] == "one of: high, normal"


def test_invalid_json_does_not_expose_raw_content():
    diagnostic = _diagnostic('{"' + PRIVATE)
    assert diagnostic["schema_error_type"] == "invalid_json"
    assert diagnostic["path"] == "root"
    assert PRIVATE not in str(diagnostic)


def test_nested_error_uses_bounded_array_path_and_no_prose():
    raw = (
        '{"headline":{"statement_key":"headline","text":"x","importance":"high",'
        '"confidence":"supported","source_ids":["a"]},"summary_statements":[],"sections":['
        '{"section_type":"current_state","statements":[{"statement_key":"x","text":"'
        + PRIVATE + '","importance":"high","confidence":7,"source_ids":["a"]}]}]}'
    )
    diagnostic = _diagnostic(raw)
    assert diagnostic["schema_error_type"] == "invalid_enum"
    assert diagnostic["path"] == "sections[0].statements[0].confidence"
    assert diagnostic["received_type"] == "integer"
    assert diagnostic["invalid_enum_value"] is None
    assert PRIVATE not in str(diagnostic)

