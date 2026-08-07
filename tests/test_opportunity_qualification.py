from pathlib import Path
from types import SimpleNamespace
import unittest

from bidlens.ingest_grants_gov import normalize_grants_gov_record
from bidlens.ingest_sam import normalize_sam_record
from bidlens.models import Opportunity
from bidlens.routes.api import _build_preview_payload
from bidlens.services.govwin import GovWinAdapter
from bidlens.services.opportunity_intake.document_extraction import FIELD_NAMES, extraction_schema
from bidlens.services.opportunity_intake.normalization import normalize_candidate
from bidlens.services.opportunity_knowledge_brief.contracts import current_state_source_id
from bidlens.services.opportunity_qualification import (
    grants_eligibility,
    qualification_presentation,
    sam_set_aside,
)


class OpportunityQualificationTests(unittest.TestCase):
    def test_eligibility_model_field_is_nullable_text(self):
        column = Opportunity.__table__.c.eligibility
        self.assertTrue(column.nullable)
        self.assertIsNone(column.default)
        self.assertEqual(column.type.__class__.__name__, "Text")

    def test_eligibility_migration_is_additive_and_has_no_backfill(self):
        migration = Path(
            "alembic/versions/a1b2c3d4e5a7_add_opportunity_eligibility.py"
        ).read_text()
        self.assertIn('down_revision = "9b0c1d2e3f4a"', migration)
        self.assertIn('op.add_column("opportunities"', migration)
        self.assertIn('sa.Column("eligibility", sa.Text(), nullable=True)', migration)
        self.assertNotIn("UPDATE opportunities", migration)

    def test_sam_uses_structured_description_then_controlled_code(self):
        self.assertEqual(sam_set_aside({
            "typeOfSetAsideDescription": "Total Small Business Set-Aside",
            "typeOfSetAside": "SBA",
        }), "Total Small Business Set-Aside")
        self.assertEqual(sam_set_aside({"typeOfSetAside": "SBA"}), "Total Small Business")
        self.assertIsNone(sam_set_aside({"title": "8(a) opportunity"}))

    def test_sam_populates_only_set_aside(self):
        normalized = normalize_sam_record({
            "noticeId": "sam-qualification", "title": "Procurement", "department": "Agency",
            "type": "Solicitation", "postedDate": "2026-08-01",
            "responseDeadLine": "2026-09-01", "uiLink": "https://sam.gov/opp/qualification",
            "typeOfSetAside": "WOSB",
        }, set())
        self.assertEqual(normalized["set_aside"], "WOSB")
        self.assertIsNone(normalized["eligibility"])

    def test_grants_eligibility_precedence_is_deterministic(self):
        payload = {
            "applicantTypes": [{"description": "Top-level applicant type"}],
            "applicantEligibilityDesc": "Top applicant",
            "additionalInformationOnEligibility": "Top additional",
            "synopsis": {
                "applicantTypes": [{"description": "Synopsis applicant type"}],
                "applicantEligibilityDesc": "Synopsis applicant",
                "additionalInformationOnEligibility": "Synopsis additional",
            },
            "forecast": {
                "applicantTypes": [{"description": "Forecast applicant type"}],
            },
        }
        self.assertEqual(grants_eligibility(payload), "Synopsis applicant type")

    def test_grants_eligibility_uses_forecast_then_top_level_applicant_types(self):
        forecast = {
            "forecast": {
                "applicantTypes": [
                    {"description": "For profit organizations other than small businesses"},
                ],
            },
            "applicantTypes": [{"description": "Top-level fallback"}],
        }
        top_level = {
            "applicantTypes": [{"description": "Top-level applicant type"}],
        }
        self.assertEqual(
            grants_eligibility(forecast),
            "For profit organizations other than small businesses",
        )
        self.assertEqual(grants_eligibility(top_level), "Top-level applicant type")

    def test_grants_eligibility_preserves_distinct_applicant_types_in_source_order(self):
        payload = {
            "synopsis": {
                "applicantTypes": [
                    {"description": "State governments"},
                    {"description": "  Nonprofits   having a 501(c)(3) status  "},
                    {"description": "state governments"},
                    {"description": "Nonprofits having a 501(c)(3) status"},
                    {"code": "99"},
                ],
            },
        }
        self.assertEqual(
            grants_eligibility(payload),
            "State governments; Nonprofits having a 501(c)(3) status",
        )

    def test_grants_eligibility_retains_scalar_fallbacks_and_null_behavior(self):
        self.assertEqual(
            grants_eligibility({
                "synopsis": {"applicantEligibilityDesc": "Synopsis applicant"},
                "applicantEligibilityDesc": "Top applicant",
            }),
            "Synopsis applicant",
        )
        self.assertEqual(
            grants_eligibility({"additionalInformationOnEligibility": "Additional details"}),
            "Additional details",
        )
        self.assertIsNone(grants_eligibility({"synopsis": {"applicantTypes": []}}))

    def test_grants_populates_only_eligibility_and_does_not_use_it_as_description(self):
        normalized, reason = normalize_grants_gov_record({
            "id": "grant-qualification", "title": "Grant", "agencyName": "Agency",
            "openDate": "08/01/2026", "closeDate": "09/01/2026",
            "fundingInstrumentDescription": "Grant",
            "synopsis": {"applicantEligibilityDesc": "State and local governments"},
        })
        self.assertIsNone(reason)
        self.assertEqual(normalized["eligibility"], "State and local governments")
        self.assertIsNone(normalized["set_aside"])
        self.assertIsNone(normalized["description"])

    def test_govwin_maps_only_explicit_qualification_fields(self):
        adapter = GovWinAdapter({})
        normalized = adapter.normalize_opportunity({
            "opportunity_id": "gw-qualification", "title": "Opportunity", "agency": "Agency",
            "posted_date": "2026-08-01", "response_deadline": "2026-09-01",
            "setAside": "HUBZone", "applicantEligibility": "Public universities",
        })
        self.assertEqual(normalized["set_aside"], "HUBZone")
        self.assertEqual(normalized["eligibility"], "Public universities")
        unknown = adapter.normalize_opportunity({
            "opportunity_id": "gw-unknown", "title": "8(a) universities", "agency": "Agency",
        })
        self.assertIsNone(unknown["set_aside"])
        self.assertIsNone(unknown["eligibility"])

    def test_intake_keeps_only_type_appropriate_qualification(self):
        contract = normalize_candidate({
            "canonical_type": "Contract", "set_aside": "8(a)", "eligibility": "Nonprofits",
        })
        grant = normalize_candidate({
            "canonical_type": "Grant", "set_aside": "8(a)", "eligibility": "Nonprofits",
        })
        unknown = normalize_candidate({"set_aside": "8(a)", "eligibility": "Nonprofits"})
        self.assertEqual((contract.set_aside, contract.eligibility), ("8(a)", None))
        self.assertEqual((grant.set_aside, grant.eligibility), (None, "Nonprofits"))
        self.assertEqual((unknown.set_aside, unknown.eligibility), (None, None))
        self.assertTrue({"set_aside", "eligibility"}.issubset(FIELD_NAMES))
        self.assertTrue({"set_aside", "eligibility"}.issubset(extraction_schema()["properties"]))

    def test_adaptive_presentation_uses_canonical_type(self):
        contract = qualification_presentation(SimpleNamespace(
            canonical_type="Contract", set_aside=None, eligibility="Nonprofits",
        ))
        grant = qualification_presentation(SimpleNamespace(
            canonical_type="Grant", set_aside="8(a)", eligibility="x" * 230,
        ))
        self.assertEqual((contract.label, contract.value), ("Set-Aside", "Not specified"))
        self.assertEqual(grant.label, "Eligibility")
        self.assertTrue(grant.is_long)
        self.assertIsNone(qualification_presentation(SimpleNamespace(
            canonical_type=None, set_aside="8(a)", eligibility="Nonprofits",
        )))

    def test_preview_api_exposes_separate_fields(self):
        payload = _build_preview_payload(SimpleNamespace(
            title="Grant", agency="Agency", canonical_type="Grant", set_aside=None,
            eligibility="Tribal governments", description=None, description_text=None,
            source_url=None, sam_url=None,
        ))
        self.assertIsNone(payload["set_aside"])
        self.assertEqual(payload["eligibility"], "Tribal governments")
        self.assertNotIn("eligibility_set_aside", payload)

    def test_templates_and_csv_keep_qualification_concepts_separate(self):
        folder = Path("src/bidlens/templates/detail.html").read_text()
        card = Path("src/bidlens/templates/_opp_card.html").read_text()
        route = Path("src/bidlens/routes/opportunities.py").read_text()
        review = Path("src/bidlens/templates/opportunity_intake_review.html").read_text()
        self.assertIn("qualification_display.label", folder)
        self.assertIn("qualification_display.label", card)
        self.assertIn('"Set-Aside",\n        "Eligibility",', route)
        self.assertIn('data-qualification-for="procurement"', review)
        self.assertIn('data-qualification-for="assistance"', review)

    def test_guts_uses_independent_controlled_eligibility_source_id(self):
        self.assertEqual(
            current_state_source_id(42, "eligibility"),
            "current_state:opportunity:42:eligibility",
        )
