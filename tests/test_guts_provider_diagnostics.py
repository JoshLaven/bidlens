from __future__ import annotations

from io import StringIO
import logging

import httpx
import openai
import pytest

from bidlens.cli import main
from bidlens.services.opportunity_knowledge_brief.model_client import (
    GUTSModelClient, GUTSModelProbeResult, ProviderErrorDiagnostic,
    probe_guts_model, sanitize_openai_provider_error,
)
from bidlens.services.opportunity_knowledge_brief.output_schema import model_probe_output_schema


SECRET = "RAW SECRET provider message sk-private prompt manifest source body"


def _status_error(cls, status, *, code=None, error_type=None, param=None):
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(status, request=request, headers={"x-request-id": "req_safe_123"})
    return cls(
        SECRET, response=response,
        body={"code": code, "type": error_type, "param": param, "message": SECRET},
    )


@pytest.mark.parametrize(("error", "subtype"), [
    (_status_error(openai.NotFoundError, 404, code="model_not_found"), "model_not_found"),
    (_status_error(openai.PermissionDeniedError, 403), "model_access_denied"),
    (_status_error(openai.BadRequestError, 400, code="unsupported_parameter", param="temperature"), "unsupported_parameter"),
    (_status_error(openai.BadRequestError, 400, code="invalid_json_schema", param="text.format.schema"), "invalid_output_schema"),
    (_status_error(openai.BadRequestError, 400), "invalid_request"),
    (_status_error(openai.RateLimitError, 429, code="rate_limit_exceeded"), "rate_limited"),
    (_status_error(openai.RateLimitError, 429, code="insufficient_quota"), "quota_exceeded"),
    (_status_error(openai.AuthenticationError, 401, code="invalid_api_key"), "authentication_failed"),
    (_status_error(openai.InternalServerError, 503, code="server_error"), "provider_unavailable"),
    (openai.APITimeoutError(request=httpx.Request("POST", "https://api.openai.com/v1/responses")), "provider_timeout"),
    (RuntimeError(SECRET), "unknown_provider_error"),
])
def test_provider_errors_are_mapped_from_safe_structured_fields(error, subtype):
    diagnostic = sanitize_openai_provider_error(error, model="gpt-test")
    rendered = str(diagnostic.as_dict())
    assert diagnostic.subtype == subtype
    assert SECRET not in rendered
    assert "sk-private" not in rendered


def test_unallowlisted_provider_metadata_is_suppressed():
    error = _status_error(
        openai.BadRequestError, 400, code="secret_code", error_type="secret_type", param="source_body",
    )
    diagnostic = sanitize_openai_provider_error(error, model="gpt-test")
    assert diagnostic.provider_code is None
    assert diagnostic.provider_type is None
    assert diagnostic.parameter is None


class _Responses:
    def __init__(self, error=None):
        self.error = error
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return object()


class _Client:
    def __init__(self, error=None):
        self.responses = _Responses(error)


def test_probe_is_evidence_free_and_reports_sanitized_failure():
    client = _Client(_status_error(openai.NotFoundError, 404, code="model_not_found"))
    result = probe_guts_model(client=client, model="gpt-test")
    assert not result.success
    assert result.diagnostic.subtype == "model_not_found"
    payload = str(client.responses.kwargs)
    assert "manifest" not in payload.casefold()
    assert "source body" not in payload.casefold()
    assert "instructions" not in client.responses.kwargs


@pytest.mark.parametrize(("model", "has_temperature"), [
    ("gpt-5.5", False),
    ("gpt-5.5-2026-08-01", False),
    ("gpt-4o-mini", True),
])
def test_probe_uses_shared_model_capability_request_builder(model, has_temperature):
    client = _Client()
    result = probe_guts_model(client=client, model=model)
    assert result.success
    assert ("temperature" in client.responses.kwargs) is has_temperature
    if has_temperature:
        assert client.responses.kwargs["temperature"] == 0
    assert client.responses.kwargs["model"] == model
    assert client.responses.kwargs["max_output_tokens"] == 32
    assert client.responses.kwargs["text"]["format"]["type"] == "json_schema"
    assert client.responses.kwargs["text"]["format"]["schema"] == model_probe_output_schema()


def test_probe_cli_does_not_open_database_and_prints_only_safe_metadata():
    diagnostic = ProviderErrorDiagnostic(
        provider="openai", model="gpt-test", subtype="model_not_found", http_status=404,
        provider_code="model_not_found", provider_type=None, parameter="model",
        request_id="req_safe_123", retryable=False,
        safe_explanation="The configured model was not found by the provider.",
    )
    output = StringIO()
    status = main(
        ["probe-guts-model", "--model", "gpt-test"], output=output,
        session_factory=lambda: pytest.fail("probe accessed the database"),
        probe_factory=lambda **kwargs: GUTSModelProbeResult(False, "openai", kwargs["model"], diagnostic),
    )
    rendered = output.getvalue()
    assert status == 1
    assert "Subtype: model_not_found" in rendered
    assert SECRET not in rendered


def test_model_client_log_and_error_never_include_raw_provider_message(monkeypatch, caplog):
    monkeypatch.setattr("bidlens.services.opportunity_knowledge_brief.model_client.config.GUTS_AI_PROVIDER", "openai")
    monkeypatch.setattr("bidlens.services.opportunity_knowledge_brief.model_client.manifest_input", lambda *args, **kwargs: "safe")
    monkeypatch.setattr("bidlens.services.opportunity_knowledge_brief.model_client.guts_output_schema", lambda **kwargs: {})
    error = _status_error(openai.BadRequestError, 400, code="unsupported_parameter", param="temperature")
    client = GUTSModelClient(client=_Client(error), model="gpt-test")
    caplog.set_level(logging.WARNING)
    with pytest.raises(Exception) as caught:
        manifest = type("Manifest", (), {
            "allowed_source_ids": lambda self: (), "manifest_version": "test",
        })()
        client._call(manifest, validation_feedback=None)
    rendered = caplog.text + str(caught.value)
    assert "provider_error_subtype=unsupported_parameter" in rendered
    assert SECRET not in rendered
    assert "sk-private" not in rendered
