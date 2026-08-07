import unittest
from types import SimpleNamespace

from bidlens.ingest_grants_gov import normalize_grants_gov_record
from bidlens.ingest_sam import normalize_sam_record
from bidlens.models import Opportunity
from bidlens.routes.api import _build_preview_payload
from bidlens.services.govwin import GovWinAdapter
from bidlens.services.opportunity_types import (
    CANONICAL_OPPORTUNITY_TYPES,
    grants_canonical_type,
    govwin_api_canonical_type,
    govwin_spreadsheet_canonical_type,
)


class CanonicalOpportunityTypeTests(unittest.TestCase):
    def test_model_declares_canonical_type_nullable_without_a_default(self):
        column = Opportunity.__table__.c.canonical_type
        self.assertTrue(column.nullable)
        self.assertIsNone(column.default)

    def test_grants_structured_instrument_maps_independently_of_stage(self):
        base = {
            "id": "grant-1", "title": "Program", "agencyName": "Agency",
            "openDate": "08/01/2026", "closeDate": "09/01/2026",
            "docType": "forecast", "oppStatus": "forecasted",
        }
        for raw, expected in (("Grant", "Grant"), ("cooperative agreement", "Cooperative Agreement")):
            with self.subTest(raw=raw):
                normalized, reason = normalize_grants_gov_record({**base, "fundingInstrumentDescription": raw})
                self.assertIsNone(reason)
                self.assertEqual(normalized["opportunity_type"], "Forecast")
                self.assertEqual(normalized["canonical_type"], expected)

    def test_grants_missing_or_unsupported_instrument_is_unclassified(self):
        self.assertIsNone(grants_canonical_type({"docType": "posted"}))
        self.assertIsNone(grants_canonical_type({"fundingInstrumentDescription": "Other"}))

    def test_sam_procurement_is_contract_and_title_does_not_create_task_order(self):
        record = {
            "noticeId": "sam-1", "title": "Task Order keywords only", "department": "Agency",
            "type": "Solicitation", "postedDate": "2026-08-01",
            "responseDeadLine": "2026-09-01", "uiLink": "https://sam.gov/opp/sam-1",
        }
        normalized = normalize_sam_record(record, set())
        self.assertEqual(normalized["canonical_type"], "Contract")

    def test_govwin_maps_only_explicit_supported_mechanisms(self):
        self.assertEqual(govwin_api_canonical_type({"awardType": "Contract"}), "Contract")
        self.assertEqual(govwin_api_canonical_type({"award_mechanism": "Task Order"}), "Task Order")
        for value in ("Delivery Order", "BPA Call", "Purchase Order", "IDIQ", "OTA"):
            with self.subTest(value=value):
                self.assertIsNone(govwin_api_canonical_type({"awardType": value}))
                self.assertIsNone(govwin_spreadsheet_canonical_type({"Award Type": value}))

    def test_govwin_lifecycle_type_is_not_treated_as_canonical_type(self):
        normalized = GovWinAdapter({}).normalize_opportunity({
            "opportunity_id": "gw-1", "title": "Task Order words", "agency": "Agency",
            "opportunity_type": "Post-RFP", "posted_date": "2026-08-01",
            "response_deadline": "2026-09-01",
        })
        self.assertIsNone(normalized["canonical_type"])

    def test_vocabulary_is_closed(self):
        self.assertEqual(CANONICAL_OPPORTUNITY_TYPES, (
            "Grant", "Cooperative Agreement", "Contract", "Task Order",
        ))

    def test_preview_payload_exposes_canonical_type_in_every_state(self):
        cases = (
            ("text", "Contract", "Source description", None),
            ("sam_fallback", "Grant", None, "https://example.test/source"),
            ("empty", None, None, None),
        )
        existing_fields = {
            "ok", "state", "title", "agency", "agency_display", "description",
            "sam_url", "source_url",
        }
        for state, canonical_type, description, source_url in cases:
            with self.subTest(state=state):
                opportunity = SimpleNamespace(
                    title="Preview opportunity",
                    agency="Agency",
                    canonical_type=canonical_type,
                    description=description,
                    description_text=None,
                    sam_url=None,
                    source_url=source_url,
                )
                payload = _build_preview_payload(opportunity)
                self.assertEqual(payload["state"], state)
                self.assertEqual(payload["canonical_type"], canonical_type)
                self.assertTrue(existing_fields.issubset(payload))
