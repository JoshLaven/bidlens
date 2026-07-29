import unittest
from datetime import date, datetime

from bidlens.services.opportunity_intake import (
    DEFAULT_ADD_TO_SHORTLIST,
    INTAKE_DECISION_STATE,
    INTAKE_QUALIFICATION_STATUS,
    INTAKE_SOURCE,
    IntakeCandidate,
    IntakeMethod,
    OpportunityPublishCommand,
    format_internal_reference,
    normalize_candidate,
    opportunity_creation_defaults,
    opportunity_field_values,
    validate_candidate,
)


class OpportunityIntakeFoundationTests(unittest.TestCase):
    def test_intake_methods_converge_on_one_candidate_contract(self):
        candidate = IntakeCandidate(title="Example", client="Agency")
        for method in IntakeMethod:
            command = OpportunityPublishCommand(
                organization_id=1,
                workspace_id=2,
                user_id=3,
                intake_method=method,
                candidate=candidate,
            )
            self.assertIs(command.candidate, candidate)
            self.assertTrue(command.add_to_shortlist)

    def test_publication_constants_are_direct_to_feed(self):
        self.assertEqual(INTAKE_SOURCE, "user_intake")
        self.assertEqual(INTAKE_QUALIFICATION_STATUS, "qualified")
        self.assertEqual(INTAKE_DECISION_STATE, "INBOX")
        self.assertTrue(DEFAULT_ADD_TO_SHORTLIST)

    def test_normalizes_text_and_supported_date_inputs(self):
        candidate = normalize_candidate({
            "title": "  Data   Platform  ",
            "client": "  Department of State ",
            "response_deadline": "08/31/2026",
            "solicitation_number": "  PAS-001  ",
            "description": " First\n\n paragraph ",
        })
        self.assertEqual(candidate.title, "Data Platform")
        self.assertEqual(candidate.client, "Department of State")
        self.assertEqual(candidate.response_deadline, date(2026, 8, 31))
        self.assertEqual(candidate.solicitation_number, "PAS-001")
        self.assertEqual(candidate.description, "First\n\nparagraph")
        self.assertEqual(
            normalize_candidate({"response_deadline": datetime(2026, 9, 1, 10, 30)}).response_deadline,
            date(2026, 9, 1),
        )

    def test_validation_requires_review_fields(self):
        result = validate_candidate({})
        self.assertFalse(result.is_valid)
        self.assertEqual(
            {error.field for error in result.errors},
            {"title", "client", "response_deadline"},
        )

    def test_missing_solicitation_number_uses_internal_reference(self):
        result = validate_candidate({
            "title": "Opportunity",
            "client": "Client",
            "response_deadline": "2026-08-31",
            "solicitation_number": " ",
        })
        self.assertTrue(result.is_valid)
        self.assertTrue(result.requires_internal_reference)

    def test_supplied_solicitation_number_is_preserved(self):
        result = validate_candidate({
            "title": "Opportunity",
            "client": "Client",
            "response_deadline": date(2026, 8, 31),
            "solicitation_number": "RFP-42",
        })
        self.assertTrue(result.is_valid)
        self.assertFalse(result.requires_internal_reference)
        self.assertEqual(result.candidate.solicitation_number, "RFP-42")

    def test_invalid_deadline_is_rejected(self):
        result = validate_candidate({
            "title": "Opportunity",
            "client": "Client",
            "response_deadline": "not-a-date",
        })
        self.assertFalse(result.is_valid)
        self.assertEqual(result.errors[0].field, "response_deadline")

    def test_model_defaults_are_explicit_and_do_not_use_triage(self):
        saved_on = date(2026, 7, 28)
        self.assertEqual(
            opportunity_creation_defaults(saved_on=saved_on),
            {
                "source": "user_intake",
                "posted_date": saved_on,
                "opportunity_type": "RFP",
            },
        )

    def test_review_client_maps_to_existing_agency_field(self):
        candidate = IntakeCandidate(
            title="Opportunity",
            client="Department of State",
            response_deadline=date(2026, 8, 31),
            description="Source description",
        )
        values = opportunity_field_values(
            candidate,
            saved_on=date(2026, 7, 28),
            source_record_id="BL-2026-000123",
            solicitation_number="BL-2026-000123",
        )
        self.assertEqual(values["agency"], "Department of State")
        self.assertEqual(values["source"], "user_intake")
        self.assertEqual(values["source_record_id"], "BL-2026-000123")
        self.assertEqual(values["solicitation_number"], "BL-2026-000123")
        self.assertEqual(values["description_text"], "Source description")

    def test_internal_reference_uses_draft_sequence(self):
        self.assertEqual(format_internal_reference(123, year=2026), "BL-2026-000123")
        self.assertEqual(format_internal_reference(1_234_567, year=2026), "BL-2026-1234567")
        for invalid in (0, -1, True, 1.5):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    format_internal_reference(invalid, year=2026)


if __name__ == "__main__":
    unittest.main()
