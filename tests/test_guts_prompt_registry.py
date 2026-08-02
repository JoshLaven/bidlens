import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bidlens.services.opportunity_knowledge_brief.model_client import GUTSModelClient, GUTSModelError
from bidlens.services.opportunity_knowledge_brief.prompt import (
    GUTS_V8_SYSTEM_INSTRUCTIONS,
    GUTSPromptConfigurationError,
    GUTSPromptDefinition,
    PROMPT_BUILDERS,
    SYSTEM_INSTRUCTIONS,
    manifest_input,
    resolve_prompt,
)
from bidlens.services.opportunity_knowledge_brief.service import (
    GUTSServiceError, OpportunityKnowledgeBriefService,
)


class FakeManifest:
    manifest_version = "guts-manifest-v1"

    @staticmethod
    def allowed_source_ids():
        return ("current_state:opportunity:1:source_stage",)

    @staticmethod
    def required_current_state_citations():
        return {
            "response_deadline": "current_state:opportunity:1:response_deadline",
            "solicitation_number": "current_state:opportunity:1:solicitation_number",
            "source_stage": "current_state:opportunity:1:source_stage",
        }

    @staticmethod
    def serializable_dict():
        return {
            "manifest_version": "guts-manifest-v1",
            "briefing_goal": "Synthetic prompt-registry fixture.",
            "evidence": {"sources": []},
        }


class FakeResponses:
    def __init__(self):
        self.requests = []

    def create(self, **request):
        self.requests.append(request)
        output = {
            "headline": {
                "statement_key": "headline", "text": "The opportunity remains active.",
                "importance": "high", "confidence": "supported",
                "source_ids": ["current_state:opportunity:1:source_stage"],
            },
            "summary_statements": [{
                "statement_key": "summary-1", "text": "The opportunity remains active.",
                "importance": "normal", "confidence": "supported",
                "source_ids": ["current_state:opportunity:1:source_stage"],
            }],
            "sections": [],
        }
        return SimpleNamespace(
            output_text=json.dumps(output),
            usage=SimpleNamespace(input_tokens=10, output_tokens=10, total_tokens=20),
        )


class FakeOpenAIClient:
    def __init__(self):
        self.responses = FakeResponses()


class GUTSPromptRegistryTests(unittest.TestCase):
    def test_guts_v8_resolves_to_frozen_current_instructions(self):
        prompt = resolve_prompt("guts-v8")
        self.assertEqual(prompt.version, "guts-v8")
        self.assertEqual(prompt.instructions, SYSTEM_INSTRUCTIONS)
        self.assertEqual(prompt.instructions, GUTS_V8_SYSTEM_INSTRUCTIONS)
        self.assertEqual(
            hashlib.sha256(prompt.instructions.encode()).hexdigest(),
            "9854a162d1976e0fe160c6d8e23bcc2ebda060f17a7b06de8b46c7651926bf08",
        )

    def test_unknown_version_fails_with_controlled_content_free_error(self):
        with self.assertRaises(GUTSPromptConfigurationError) as raised:
            resolve_prompt("unknown-prompt-containing-private-source-text")
        self.assertEqual(raised.exception.safe_category, "model_configuration_missing")
        self.assertEqual(str(raised.exception), "GUTS prompt configuration is invalid.")
        self.assertNotIn("unknown-prompt", str(raised.exception))

        with patch(
            "bidlens.services.opportunity_knowledge_brief.model_client.config.GUTS_PROMPT_VERSION",
            "unknown-prompt-containing-private-source-text",
        ):
            with self.assertRaises(GUTSModelError) as model_error:
                GUTSModelClient(client=FakeOpenAIClient(), model="test-model")
        self.assertEqual(model_error.exception.safe_category, "model_configuration_missing")
        self.assertNotIn("unknown-prompt", model_error.exception.safe_message)

    def test_service_rejects_unknown_version_before_database_or_generation_work(self):
        db = MagicMock()
        service = OpportunityKnowledgeBriefService(db)
        with (
            patch(
                "bidlens.services.opportunity_knowledge_brief.service.config.GUTS_ENABLED", True,
            ),
            patch(
                "bidlens.services.opportunity_knowledge_brief.service.config.GUTS_PROMPT_VERSION",
                "unknown-prompt-containing-private-source-text",
            ),
        ):
            with self.assertRaises(GUTSServiceError) as raised:
                service.generate(
                    opportunity_id=1, requesting_user=SimpleNamespace(id=1),
                    active_organization_id=1,
                )
        self.assertEqual(raised.exception.safe_category, "model_configuration_missing")
        self.assertEqual(raised.exception.stage, "configuration")
        self.assertNotIn("unknown-prompt", raised.exception.safe_message)
        db.assert_not_called()

    def test_registry_selection_changes_instructions_not_only_metadata(self):
        alternate = GUTSPromptDefinition(
            version="test-prompt", instructions="Synthetic alternate instructions.",
            output_schema_version="guts-output-v2",
        )
        builders = {**PROMPT_BUILDERS, "test-prompt": lambda: alternate}
        client = FakeOpenAIClient()
        with (
            patch.dict(PROMPT_BUILDERS, builders, clear=True),
            patch(
                "bidlens.services.opportunity_knowledge_brief.model_client.config.GUTS_PROMPT_VERSION",
                "test-prompt",
            ),
        ):
            model = GUTSModelClient(client=client, model="test-model")
            model.generate(FakeManifest())

        request = client.responses.requests[0]
        self.assertEqual(request["instructions"], "Synthetic alternate instructions.")
        self.assertEqual(request["metadata"]["prompt_version"], "test-prompt")
        self.assertEqual(json.loads(request["input"])["prompt_version"], "test-prompt")

    def test_initial_and_corrective_retry_use_same_resolved_prompt(self):
        client = FakeOpenAIClient()
        model = GUTSModelClient(client=client, model="test-model")
        model.generate(FakeManifest())
        with patch(
            "bidlens.services.opportunity_knowledge_brief.prompt.config.GUTS_PROMPT_VERSION",
            "unregistered-after-client-construction",
        ):
            model.retry_with_validation_feedback(
                FakeManifest(), "Use statement_key 'headline' for the headline.",
            )

        first, retry = client.responses.requests
        self.assertEqual(first["instructions"], retry["instructions"])
        self.assertEqual(first["metadata"]["prompt_version"], "guts-v9")
        self.assertEqual(retry["metadata"]["prompt_version"], "guts-v9")
        self.assertEqual(json.loads(first["input"])["prompt_version"], "guts-v9")
        self.assertEqual(json.loads(retry["input"])["prompt_version"], "guts-v9")
        self.assertEqual(
            json.loads(retry["input"])["validation_feedback"],
            "Use statement_key 'headline' for the headline.",
        )

    def test_manifest_input_uses_explicit_selected_prompt(self):
        prompt = GUTSPromptDefinition("fixture-version", "Fixture instructions")
        payload = json.loads(manifest_input(FakeManifest(), prompt=prompt))
        self.assertEqual(payload["prompt_version"], "fixture-version")

if __name__ == "__main__":
    unittest.main()
