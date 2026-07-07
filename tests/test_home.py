import datetime as dt
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import (
    CompanyProfile,
    Event,
    IngestionRun,
    Opportunity,
    Organization,
    OrganizationMembership,
    PursuitLane,
    SamSourceConfig,
    User,
)
from bidlens.routes.home import go_live
from bidlens.services.home import get_home_context


class HomeContextTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Home Org", slug="home-org")
        self.other_org = Organization(name="Other Org", slug="other-home-org")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.admin = User(email="admin@home.test", organization_id=self.org.id)
        self.db.add(self.admin)
        self.db.flush()
        self.db.add(OrganizationMembership(
            organization_id=self.org.id,
            user_id=self.admin.id,
            role="admin",
        ))
        self.db.commit()
        self.now = dt.datetime(2026, 7, 6, 16, 0, tzinfo=dt.timezone.utc)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _context(self, *, salesforce_connected=False):
        return get_home_context(
            self.db,
            self.org.id,
            self.admin.id,
            now=self.now,
            salesforce_connected=salesforce_connected,
        )

    def _profile(self):
        self.db.add(CompanyProfile(
            org_id=self.org.id,
            company_name="Home Org",
            profile_json={"company_overview": "Public-sector research"},
        ))
        self.db.commit()

    def _source(self):
        self.db.add(SamSourceConfig(
            organization_id=self.org.id,
            name="Primary federal search",
            naics_codes=["541611"],
        ))
        self.db.commit()

    def _opportunity(self, **overrides):
        values = {
            "organization_id": self.org.id,
            "source": "sam.gov",
            "source_record_id": "HOME-1",
            "title": "Home opportunity",
            "agency": "Test Agency",
            "opportunity_type": "Solicitation",
            "posted_date": dt.date(2026, 7, 1),
            "response_deadline": dt.date(2026, 8, 1),
            "qualification_status": "unreviewed",
            "decision_state": "INBOX",
        }
        values.update(overrides)
        opportunity = Opportunity(**values)
        self.db.add(opportunity)
        self.db.commit()
        return opportunity

    def test_empty_workspace_shows_only_applicable_setup_actions(self):
        context = self._context()
        steps = {item["key"]: item for item in context["next_steps"]}

        self.assertEqual(
            list(steps),
            ["company-profile", "opportunity-source", "invite-team", "salesforce", "pursuit-lanes"],
        )
        self.assertEqual(steps["company-profile"]["label"], "Required")
        self.assertEqual(steps["salesforce"]["label"], "Optional")
        self.assertNotIn("first-import", steps)
        self.assertNotIn("first-review", steps)
        self.assertFalse(context["workspace_summary"]["required_setup_complete"])
        self.assertFalse(context["is_live"])
        self.assertFalse(context["can_go_live"])
        self.assertIsNone(context["operational_home_context"])
        self.assertEqual(context["workspace_summary"]["headline"], "Welcome to BidLens.")
        self.assertIn("Organization created", [item["title"] for item in context["completed"]])
        self.assertEqual(steps["company-profile"]["title"], "Tell BidLens about your organization")
        self.assertEqual(steps["opportunity-source"]["title"], "Enable at least one opportunity source")

    def test_configured_source_without_opportunities_completes_required_setup(self):
        self._profile()
        self._source()

        context = self._context()
        steps = {item["key"]: item for item in context["next_steps"]}

        self.assertNotIn("first-import", steps)
        self.assertNotIn("opportunity-source", steps)
        self.assertEqual(context["operational_snapshot"]["sources_enabled"], 1)
        self.assertTrue(context["workspace_summary"]["required_setup_complete"])
        self.assertTrue(context["can_go_live"])
        self.assertIsNone(context["operational_home_context"])
        self.assertIn("Opportunity sources connected", [item["title"] for item in context["completed"]])

    def test_required_completion_makes_pre_live_workspace_ready_to_go_live(self):
        self._profile()
        self._source()
        self._opportunity()
        self.db.add(IngestionRun(
            source="sam.gov",
            organization_id=self.org.id,
            user_id=self.admin.id,
            started_at=dt.datetime(2026, 7, 6, 14, 0),
            finished_at=dt.datetime(2026, 7, 6, 14, 5),
            status="completed",
            error_count=0,
        ))
        self.db.commit()

        context = self._context()
        steps = {item["key"]: item for item in context["next_steps"]}

        self.assertTrue(context["workspace_summary"]["required_setup_complete"])
        self.assertFalse(context["is_live"])
        self.assertTrue(context["can_go_live"])
        self.assertEqual(context["workspace_summary"]["description"], "Let’s get your organization ready.")
        self.assertNotIn("first-review", steps)
        self.assertNotIn("first-import", steps)
        self.assertEqual(context["operational_snapshot"]["opportunities_awaiting_review"], 1)
        self.assertIsNotNone(context["operational_snapshot"]["last_successful_import"])
        self.assertIsNone(context["operational_home_context"])

    def test_live_workspace_returns_operational_home_context(self):
        self._profile()
        self._source()
        self._opportunity()
        self.org.is_live = True
        self.db.commit()

        context = self._context()

        self.assertTrue(context["is_live"])
        self.assertFalse(context["can_go_live"])
        self.assertEqual(context["workspace_summary"]["description"], "Your workspace is ready.")
        self.assertIsNotNone(context["operational_home_context"])
        self.assertEqual(
            context["operational_home_context"]["operational_snapshot"]["opportunities_awaiting_review"],
            1,
        )

    def test_go_live_route_sets_org_live_and_records_event(self):
        self._profile()
        self._source()
        setattr(self.admin, "current_organization_id", self.org.id)
        setattr(self.admin, "current_role", "admin")

        with (
            patch("bidlens.routes.home.get_current_user", return_value=self.admin),
            patch("bidlens.routes.home.attach_request_user_context", return_value=self.admin),
        ):
            response = asyncio.run(go_live(SimpleNamespace(), self.db))

        self.db.refresh(self.org)
        event = (
            self.db.query(Event)
            .filter(Event.org_id == self.org.id, Event.event_type == "workspace_went_live")
            .first()
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/home")
        self.assertTrue(self.org.is_live)
        self.assertIsNotNone(event)

    def test_completed_recommendations_disappear_and_salesforce_remains_optional(self):
        self._profile()
        self._source()
        self._opportunity(decision_state="SHORTLISTED", qualification_status="qualified")
        second_user = User(email="member@home.test", organization_id=self.org.id)
        self.db.add(second_user)
        self.db.flush()
        self.db.add_all([
            OrganizationMembership(
                organization_id=self.org.id,
                user_id=second_user.id,
                role="member",
            ),
            PursuitLane(
                organization_id=self.org.id,
                name="Health",
                agencies=[],
                naics=[],
                keywords=[],
                set_asides=[],
            ),
        ])
        self.db.commit()

        disconnected = self._context(salesforce_connected=False)
        connected = self._context(salesforce_connected=True)

        self.assertEqual([step["key"] for step in disconnected["next_steps"]], ["salesforce"])
        self.assertTrue(disconnected["workspace_summary"]["required_setup_complete"])
        self.assertEqual(connected["next_steps"], [])
        self.assertTrue(connected["operational_snapshot"]["salesforce_connected"])

    def test_latest_failed_connector_run_creates_attention_item(self):
        self._profile()
        self._source()
        self._opportunity(decision_state="ARCHIVED")
        self.db.add(IngestionRun(
            source="sam.gov",
            organization_id=self.org.id,
            user_id=self.admin.id,
            started_at=dt.datetime(2026, 7, 6, 15, 0),
            finished_at=dt.datetime(2026, 7, 6, 15, 1),
            status="failed",
            error_count=1,
            notes="SAM.gov returned an authentication error.",
        ))
        self.db.commit()

        context = self._context()

        self.assertEqual(context["operational_snapshot"]["connector_issues"], 1)
        self.assertEqual(len(context["attention_items"]), 1)
        self.assertIn("authentication error", context["attention_items"][0]["description"])

    def test_other_organization_records_do_not_change_workspace_state(self):
        self.db.add_all([
            CompanyProfile(
                org_id=self.other_org.id,
                company_name="Other Org",
                profile_json={"company_overview": "Other"},
            ),
            SamSourceConfig(
                organization_id=self.other_org.id,
                name="Other search",
                naics_codes=["999999"],
            ),
        ])
        self.db.commit()

        context = self._context()
        step_keys = {item["key"] for item in context["next_steps"]}

        self.assertIn("company-profile", step_keys)
        self.assertIn("opportunity-source", step_keys)
        self.assertEqual(context["operational_snapshot"]["sources_enabled"], 0)


if __name__ == "__main__":
    unittest.main()
