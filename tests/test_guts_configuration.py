import json
import os
import subprocess
import sys
import unittest

from bidlens import config
from bidlens.services.opportunity_knowledge_brief import (
    AUTHORITIES,
    CONFIDENCE_VALUES,
    FAILURE_CATEGORIES,
    GENERATION_STATUSES,
    IMPORTANCE_VALUES,
    PLACEMENT_TYPES,
    REPRODUCIBILITY_STATUSES,
    SECTION_TYPES,
    SOURCE_CLASSES,
    WARNING_TYPES,
)


class GutsConfigurationTests(unittest.TestCase):
    def test_guts_is_disabled_by_default_and_uses_dedicated_defaults(self):
        self.assertFalse(config.GUTS_ENABLED)
        self.assertEqual(config.GUTS_AI_PROVIDER, "openai")
        self.assertEqual(config.GUTS_AI_MODEL, config.OPENAI_MODEL)
        self.assertEqual(config.GUTS_MAX_RETRIES, 1)
        self.assertGreater(config.GUTS_TIMEOUT_SECONDS, 0)
        self.assertGreater(config.GUTS_MAX_OUTPUT_TOKENS, 0)
        self.assertGreater(config.GUTS_MAX_TOTAL_INPUT_CHARS, 0)
        self.assertGreater(config.GUTS_MAX_NOTES, 0)
        self.assertGreater(config.GUTS_MAX_NOTE_CHARS, 0)
        self.assertGreater(config.GUTS_MAX_TOTAL_NOTE_CHARS, 0)
        self.assertGreater(config.GUTS_MAX_MESSAGES, 0)
        self.assertGreater(config.GUTS_MAX_MESSAGE_CHARS, 0)
        self.assertGreater(config.GUTS_MAX_TOTAL_COMMUNICATION_CHARS, 0)
        self.assertGreater(config.GUTS_MAX_HISTORY_EVENTS, 0)
        self.assertGreater(config.GUTS_MAX_TOTAL_HISTORY_CHARS, 0)
        self.assertGreater(config.GUTS_MAX_OFFICIAL_DOCUMENTS, 0)
        self.assertGreater(config.GUTS_MAX_OFFICIAL_DOC_CHARS, 0)
        self.assertGreater(config.GUTS_MAX_TOTAL_OFFICIAL_CHARS, 0)
        self.assertGreater(config.GUTS_MAX_SUMMARY_STATEMENTS, 0)
        self.assertGreater(config.GUTS_MAX_SECTIONS, 0)
        self.assertGreater(config.GUTS_MAX_STATEMENTS_PER_SECTION, 0)
        self.assertGreater(config.GUTS_MAX_STATEMENT_CHARS, 0)
        self.assertGreater(config.GUTS_MAX_TOTAL_OUTPUT_CHARS, 0)
        self.assertLessEqual(
            config.GUTS_MAX_TOTAL_NOTE_CHARS + config.GUTS_MAX_TOTAL_COMMUNICATION_CHARS,
            config.GUTS_MAX_TOTAL_INPUT_CHARS,
        )
        self.assertIs(config.OPENAI_API_KEY, config.OPENAI_API_KEY)
        self.assertFalse(hasattr(config, "GUTS_API_KEY"))

    def test_environment_overrides_are_parsed(self):
        env = dict(os.environ)
        env.update({
            "PYTHONPATH": "src",
            "GUTS_ENABLED": "true",
            "GUTS_AI_PROVIDER": "TEST_PROVIDER",
            "GUTS_AI_MODEL": "test-model",
            "GUTS_PROMPT_VERSION": "p9",
            "GUTS_MANIFEST_VERSION": "m9",
            "GUTS_OUTPUT_SCHEMA_VERSION": "s9",
            "GUTS_TIMEOUT_SECONDS": "12.5",
            "GUTS_MAX_OUTPUT_TOKENS": "321",
            "GUTS_MAX_RETRIES": "2",
            "GUTS_STALE_ATTEMPT_SECONDS": "44",
            "GUTS_MAX_TOTAL_INPUT_CHARS": "9876",
            "GUTS_MAX_NOTES": "9",
            "GUTS_MAX_NOTE_CHARS": "876",
            "GUTS_MAX_TOTAL_NOTE_CHARS": "7654",
            "GUTS_MAX_MESSAGES": "8",
            "GUTS_MAX_MESSAGE_CHARS": "654",
            "GUTS_MAX_TOTAL_COMMUNICATION_CHARS": "5432",
            "GUTS_MAX_HISTORY_EVENTS": "7",
            "GUTS_MAX_TOTAL_HISTORY_CHARS": "4321",
            "GUTS_MAX_OFFICIAL_DOCUMENTS": "4",
            "GUTS_MAX_OFFICIAL_DOC_CHARS": "3210",
            "GUTS_MAX_TOTAL_OFFICIAL_CHARS": "8765",
            "GUTS_MAX_SUMMARY_STATEMENTS": "3",
            "GUTS_MAX_SECTIONS": "4",
            "GUTS_MAX_STATEMENTS_PER_SECTION": "5",
            "GUTS_MAX_STATEMENT_CHARS": "444",
            "GUTS_MAX_TOTAL_OUTPUT_CHARS": "3333",
        })
        code = (
            "import json; from bidlens import config; "
            "print(json.dumps({k:getattr(config,k) for k in ["
            "'GUTS_ENABLED','GUTS_AI_PROVIDER','GUTS_AI_MODEL','GUTS_PROMPT_VERSION',"
            "'GUTS_MANIFEST_VERSION','GUTS_OUTPUT_SCHEMA_VERSION','GUTS_TIMEOUT_SECONDS',"
            "'GUTS_MAX_OUTPUT_TOKENS','GUTS_MAX_RETRIES','GUTS_STALE_ATTEMPT_SECONDS',"
            "'GUTS_MAX_TOTAL_INPUT_CHARS','GUTS_MAX_NOTES','GUTS_MAX_NOTE_CHARS',"
            "'GUTS_MAX_TOTAL_NOTE_CHARS','GUTS_MAX_MESSAGES','GUTS_MAX_MESSAGE_CHARS',"
            "'GUTS_MAX_TOTAL_COMMUNICATION_CHARS','GUTS_MAX_HISTORY_EVENTS',"
            "'GUTS_MAX_TOTAL_HISTORY_CHARS','GUTS_MAX_OFFICIAL_DOCUMENTS',"
            "'GUTS_MAX_OFFICIAL_DOC_CHARS','GUTS_MAX_TOTAL_OFFICIAL_CHARS',"
            "'GUTS_MAX_SUMMARY_STATEMENTS','GUTS_MAX_SECTIONS',"
            "'GUTS_MAX_STATEMENTS_PER_SECTION','GUTS_MAX_STATEMENT_CHARS',"
            "'GUTS_MAX_TOTAL_OUTPUT_CHARS']}))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], env=env, cwd=os.getcwd(), check=True,
            capture_output=True, text=True, timeout=20,
        )
        values = json.loads(result.stdout)
        self.assertTrue(values["GUTS_ENABLED"])
        self.assertEqual(values["GUTS_AI_PROVIDER"], "test_provider")
        self.assertEqual(values["GUTS_AI_MODEL"], "test-model")
        self.assertEqual(values["GUTS_TIMEOUT_SECONDS"], 12.5)
        self.assertEqual(values["GUTS_MAX_RETRIES"], 2)
        self.assertEqual(values["GUTS_MAX_NOTES"], 9)
        self.assertEqual(values["GUTS_MAX_TOTAL_COMMUNICATION_CHARS"], 5432)
        self.assertEqual(values["GUTS_MAX_HISTORY_EVENTS"], 7)
        self.assertEqual(values["GUTS_MAX_TOTAL_OFFICIAL_CHARS"], 8765)
        self.assertEqual(values["GUTS_MAX_SUMMARY_STATEMENTS"], 3)
        self.assertEqual(values["GUTS_MAX_TOTAL_OUTPUT_CHARS"], 3333)

    def test_constant_contracts_match_locked_values(self):
        self.assertEqual(GENERATION_STATUSES, {"pending", "running", "succeeded", "failed"})
        self.assertEqual(REPRODUCIBILITY_STATUSES, {
            "fully_reproducible", "partially_reproducible", "not_reproducible",
        })
        self.assertEqual(SOURCE_CLASSES, {
            "current_state", "official_evidence", "organizational_knowledge", "historical_context",
        })
        self.assertEqual(AUTHORITIES, {
            "authoritative_current", "official_source", "attributed_claim", "historical_record",
        })
        self.assertEqual(PLACEMENT_TYPES, {"headline", "summary", "section"})
        self.assertEqual(SECTION_TYPES, {
            "current_state", "official_updates", "organizational_knowledge", "important_history", "uncertainties",
        })
        self.assertEqual(IMPORTANCE_VALUES, {"high", "normal"})
        self.assertEqual(CONFIDENCE_VALUES, {"supported", "attributed", "uncertain"})
        self.assertEqual(WARNING_TYPES, {
            "missing_source", "partial_generation", "conflicting_sources", "truncated_input", "not_fully_reproducible",
        })
        self.assertIn("generation_already_in_progress", FAILURE_CATEGORIES)
        self.assertIn("model_citation_invalid", FAILURE_CATEGORIES)
        self.assertIn("unexpected_error", FAILURE_CATEGORIES)


if __name__ == "__main__":
    unittest.main()
