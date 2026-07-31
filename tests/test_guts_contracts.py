import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from bidlens.services.opportunity_knowledge_brief.contracts import (
    CurrentOpportunityState,
    CurrentStateField,
    EvidenceSelection,
    EvidenceSelectionStatistics,
    GenerationConstraints,
    GUTSManifest,
    ModelStatement,
    SalesforceLinkState,
    current_state_source_id,
)


def field(name, value=None):
    return CurrentStateField(value=value, source_id=f"current_state:opportunity:554:{name}")


def current_state():
    return CurrentOpportunityState(
        opportunity_id=554,
        organization_id=1,
        workspace_id=2,
        title=field("title", "Opportunity"),
        client=field("client", "Agency"),
        description=field("description"),
        response_deadline=field("response_deadline", "2026-09-01"),
        posted_date=field("posted_date", "2026-07-31"),
        solicitation_number=field("solicitation_number", "RFP-1"),
        opportunity_type=field("opportunity_type", "RFP"),
        source_stage=field("source_stage"),
        source=field("source", "sam"),
        source_record_id=field("source_record_id", "notice-1"),
        source_url=field("source_url"),
        sam_url=field("sam_url"),
        bidlens_id=field("bidlens_id", "00000000-0000-0000-0000-000000000554"),
        sam_notice_id=field("sam_notice_id", "notice-1"),
        naics=field("naics"),
        naics_title=field("naics_title"),
        set_aside=field("set_aside"),
        description_original_character_count=0,
        description_was_truncated=False,
        salesforce=SalesforceLinkState(linked=False),
    )


class GutsContractTests(unittest.TestCase):
    def test_unknown_fields_and_uncontrolled_values_are_rejected(self):
        with self.assertRaises(ValidationError):
            CurrentStateField(value="x", source_id="source", extra_field="no")
        with self.assertRaises(ValidationError):
            ModelStatement(
                statement_key="s1", placement_type="sidebar", position=0, text="Text",
                importance="high", confidence="supported", source_ids=("source:1",),
            )
        with self.assertRaises(ValidationError):
            ModelStatement(
                statement_key="s1", placement_type="headline", position=0, text="Text",
                importance="high", confidence="supported", source_ids=(),
            )

    def test_serialization_is_deterministic_and_datetime_conversion_is_explicit(self):
        timestamp = datetime(2026, 7, 31, 12, 30, tzinfo=timezone.utc)
        manifest = GUTSManifest(
            manifest_version="m1",
            opportunity_id=554,
            organization_id=1,
            workspace_id=2,
            snapshot_started_at=timestamp,
            snapshot_completed_at=timestamp,
            current_state=current_state(),
            evidence=EvidenceSelection(
                statistics=EvidenceSelectionStatistics(
                    available_source_count=0,
                    unavailable_source_count=0,
                    selected_character_count=0,
                    original_character_count=0,
                    truncated_source_count=0,
                )
            ),
            constraints=GenerationConstraints(
                max_total_input_characters=1000,
                max_output_tokens=500,
                timeout_seconds=30.0,
                max_retries=1,
            ),
            reproducibility_status="fully_reproducible",
        )
        first = manifest.canonical_json()
        second = manifest.canonical_json()
        self.assertEqual(first, second)
        self.assertIn('"snapshot_started_at":"2026-07-31T12:30:00Z"', first)
        self.assertNotIn("datetime.datetime", first)

    def test_contracts_are_database_free_and_strict(self):
        state = current_state()
        self.assertEqual(state.title.value, "Opportunity")
        with self.assertRaises(ValidationError):
            GenerationConstraints(
                max_total_input_characters="1000",
                max_output_tokens=500,
                timeout_seconds=30.0,
                max_retries=1,
            )

    def test_current_state_source_ids_are_stable_and_controlled(self):
        expected = "current_state:opportunity:554:response_deadline"
        self.assertEqual(current_state_source_id(554, "response_deadline"), expected)
        self.assertEqual(current_state_source_id(554, "response_deadline"), expected)
        for invalid in ("title:other", "../../notes", "custom field", "raw_source_payload"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    current_state_source_id(554, invalid)
        with self.assertRaises(ValueError):
            current_state_source_id(0, "title")


if __name__ == "__main__":
    unittest.main()
