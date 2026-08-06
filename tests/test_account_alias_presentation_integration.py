import datetime as dt
from pathlib import Path
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import Opportunity, Organization, OrganizationMembership, User, Workspace
from bidlens.routes.api import _build_preview_payload
from bidlens.routes.opportunities import _calendar_drawer_items, _enrich_opps
from bidlens.services.home import _daily_brief_section_item
from bidlens.services.opportunity_knowledge_brief.current_state import CurrentStateAssembler
from bidlens.services.shortlist_activity import shortlist_recent_activity


RAW_ACCOUNT = "Administration for Children and Families - ACYF/CB"
DISPLAY_ACCOUNT = "Administration for Children and Families"


class AccountAliasPresentationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Alias Org", slug="alias-org")
        self.db.add(self.org)
        self.db.flush()
        self.user = User(email="alias@example.test", organization_id=self.org.id)
        self.workspace = Workspace(organization_id=self.org.id, name="Alias", slug="alias")
        self.db.add_all([self.user, self.workspace])
        self.db.flush()
        self.db.add(OrganizationMembership(
            organization_id=self.org.id,
            user_id=self.user.id,
            role="member",
        ))
        self.opportunity = Opportunity(
            organization_id=self.org.id,
            source="manual_import",
            source_record_id="alias-integration",
            title="Alias integration",
            agency=RAW_ACCOUNT,
            opportunity_type="RFP",
            posted_date=dt.date(2026, 8, 1),
            response_deadline=dt.date(2026, 9, 1),
            qualification_status="qualified",
        )
        self.db.add(self.opportunity)
        self.db.commit()
        self.user.current_organization_id = self.org.id
        self.user.current_role = "member"

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_card_enrichment_resolves_display_without_rewriting_existing_record(self):
        enriched = _enrich_opps([(self.opportunity, False)], self.db, self.user)

        self.assertEqual(enriched[0].agency_display, DISPLAY_ACCOUNT)
        self.assertEqual(enriched[0].agency, RAW_ACCOUNT)
        self.assertEqual(
            self.db.get(Opportunity, self.opportunity.id).agency,
            RAW_ACCOUNT,
        )

    def test_preview_api_preserves_raw_and_adds_resolved_display(self):
        payload = _build_preview_payload(self.opportunity)

        self.assertEqual(payload["agency"], RAW_ACCOUNT)
        self.assertEqual(payload["agency_display"], DISPLAY_ACCOUNT)

    def test_recent_activity_and_calendar_use_resolved_display(self):
        activity = shortlist_recent_activity(
            self.db,
            organization_id=self.org.id,
            opportunities=[self.opportunity],
        )
        calendar = _calendar_drawer_items([(self.opportunity, False)])

        self.assertEqual(activity[str(self.opportunity.id)]["opportunity"]["agency"], DISPLAY_ACCOUNT)
        self.assertEqual(calendar[0]["agency"], DISPLAY_ACCOUNT)

    def test_daily_brief_resolves_presentation_without_mutating_snapshot_payload(self):
        raw_item = {
            "opportunity": {
                "id": self.opportunity.id,
                "title": self.opportunity.title,
                "agency": RAW_ACCOUNT,
                "response_deadline": "2026-09-01",
            }
        }

        item = _daily_brief_section_item("new_opportunities", raw_item)

        self.assertIn(DISPLAY_ACCOUNT, item["subtitle"])
        self.assertEqual(raw_item["opportunity"]["agency"], RAW_ACCOUNT)

    def test_guts_current_state_uses_resolved_client_without_rewriting_source(self):
        state = CurrentStateAssembler(self.db).build(
            opportunity=self.opportunity,
            organization_id=self.org.id,
            workspace_id=self.workspace.id,
        )

        self.assertEqual(state.client.value, DISPLAY_ACCOUNT)
        self.assertEqual(self.opportunity.agency, RAW_ACCOUNT)

    def test_folder_and_csv_boundaries_use_display_and_preserve_source_columns(self):
        route_source = Path("src/bidlens/routes/opportunities.py").read_text()
        detail_template = Path("src/bidlens/templates/detail.html").read_text()

        self.assertIn("opportunity.agency_display = account_display", route_source)
        self.assertIn("opportunity.agency_display", detail_template)
        self.assertIn('"Account",\n        "Source Account",', route_source)
        self.assertIn("resolve_account_display_name(opp.agency),\n            opp.agency or \"\",", route_source)

    def test_phase_one_does_not_change_sql_sort_search_or_feed_rules(self):
        route_source = Path("src/bidlens/routes/opportunities.py").read_text()
        feed_query_source = Path("src/bidlens/services/feed_queries.py").read_text()

        self.assertIn('"agency": func.lower(Opportunity.agency)', route_source)
        self.assertIn("Opportunity.agency.ilike(pattern)", route_source)
        self.assertIn("Opportunity.agency.ilike(f\"%{agency}%\")", feed_query_source)


if __name__ == "__main__":
    unittest.main()
