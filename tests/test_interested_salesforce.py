import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import Opportunity, Organization, OrganizationMembership, User, Vote
from bidlens.routes import api, opportunities
from bidlens.services.salesforce import (
    SalesforceConfigError,
    SalesforceOpportunity,
)


class InterestedSalesforceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        session_factory = sessionmaker(bind=self.engine)
        self.db = session_factory()

        self.org = Organization(name="Test Org", slug="interested-salesforce-test")
        self.db.add(self.org)
        self.db.flush()
        self.user = User(email="member@example.com", organization_id=self.org.id)
        self.member = User(email="standard-member@example.com", organization_id=self.org.id)
        self.db.add_all([self.user, self.member])
        self.db.flush()
        self.db.add_all([
            OrganizationMembership(
                organization_id=self.org.id,
                user_id=self.user.id,
                role="admin",
            ),
            OrganizationMembership(
                organization_id=self.org.id,
                user_id=self.member.id,
                role="member",
            ),
        ])
        self.opp = Opportunity(
            organization_id=self.org.id,
            source="sam",
            source_record_id="notice-1001",
            title="Salesforce-aware interest",
            agency="Test Agency",
            opportunity_type="Solicitation",
            posted_date=date.today(),
            response_deadline=date.today() + timedelta(days=30),
            qualification_status="qualified",
        )
        self.db.add(self.opp)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _vote_interested(self, service):
        with (
            patch.object(api, "require_user", return_value=self.user),
            patch.object(api, "SalesforceService", return_value=service),
        ):
            return api.api_vote(
                api.VoteIn(opp_id=self.opp.id, vote="PURSUE"),
                MagicMock(),
                self.db,
            )

    def test_standard_user_interest_does_not_invoke_salesforce(self):
        service = MagicMock()
        with (
            patch.object(api, "require_user", return_value=self.member),
            patch.object(api, "SalesforceService", return_value=service),
        ):
            result = api.api_vote(
                api.VoteIn(opp_id=self.opp.id, vote="PURSUE"),
                MagicMock(),
                self.db,
            )

        self.assertEqual(result["vote"], "PURSUE")
        self.assertEqual(result["salesforce_outcome"], "not_requested")
        self.assertFalse(result["admin_crm_action"])
        service.is_authorized.assert_not_called()

    def test_sidebar_orders_most_recent_interest_first(self):
        older_opp = Opportunity(
            organization_id=self.org.id,
            source="sam",
            source_record_id="notice-older",
            title="Older shortlist choice",
            agency="Test Agency",
            opportunity_type="Solicitation",
            posted_date=date.today(),
            response_deadline=date.today() + timedelta(days=2),
            qualification_status="qualified",
        )
        self.db.add(older_opp)
        self.db.flush()
        self.db.add_all([
            Vote(
                org_id=self.org.id,
                user_id=self.user.id,
                opp_id=older_opp.id,
                vote="PURSUE",
                updated_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            ),
            Vote(
                org_id=self.org.id,
                user_id=self.user.id,
                opp_id=self.opp.id,
                vote="PURSUE",
                updated_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
            ),
        ])
        self.db.commit()

        sidebar = opportunities.get_sidebar(self.db, self.user)

        self.assertEqual(
            [opp.id for opp in sidebar["my_shortlisted"][:2]],
            [self.opp.id, older_opp.id],
        )

    def _assert_pursue_saved(self):
        vote = (
            self.db.query(Vote)
            .filter(
                Vote.org_id == self.org.id,
                Vote.user_id == self.user.id,
                Vote.opp_id == self.opp.id,
            )
            .one()
        )
        self.assertEqual(vote.vote, "PURSUE")

    def test_interested_links_existing_salesforce_opportunity(self):
        service = MagicMock()
        service.is_authorized.return_value = True
        service.find_opportunity_by_external_source_id.return_value = SalesforceOpportunity(
            id="006-existing",
            name="Existing Opportunity",
            external_source_id=self.opp.source_record_id,
            intake_status=None,
        )
        service.opportunity_record_url.return_value = (
            "https://example.my.salesforce.com/lightning/r/Opportunity/006-existing/view"
        )

        result = self._vote_interested(service)
        self.db.refresh(self.opp)

        self._assert_pursue_saved()
        self.assertEqual(result["salesforce_outcome"], "linked")
        self.assertEqual(self.opp.salesforce_opportunity_id, "006-existing")
        self.assertEqual(
            result["sidebar"]["my_shortlisted"][0]["salesforce_opportunity_url"],
            "https://example.my.salesforce.com/lightning/r/Opportunity/006-existing/view",
        )
        service.update_intake_status.assert_called_once()
        service.create_opportunity.assert_not_called()

    def test_interested_creates_when_no_match_exists(self):
        service = MagicMock()
        service.is_authorized.return_value = True
        service.find_opportunity_by_external_source_id.return_value = None
        service.required_createable_opportunity_fields.return_value = []
        service.stage_name_values.return_value = ["Prospecting"]
        service.opportunity_picklist_values.return_value = ["BidLens"]
        service.create_opportunity.return_value = "006-created"
        service.opportunity_record_url.return_value = (
            "https://example.my.salesforce.com/lightning/r/Opportunity/006-created/view"
        )

        result = self._vote_interested(service)
        self.db.refresh(self.opp)

        self._assert_pursue_saved()
        self.assertEqual(result["salesforce_outcome"], "created")
        self.assertEqual(self.opp.salesforce_opportunity_id, "006-created")
        payload = service.create_opportunity.call_args.args[0]
        self.assertEqual(payload["External_Source_ID_c__c"], "notice-1001")

    def test_repeated_interest_does_not_create_duplicate_or_reenter_feed(self):
        service = MagicMock()
        service.is_authorized.return_value = True
        service.find_opportunity_by_external_source_id.return_value = None
        service.required_createable_opportunity_fields.return_value = []
        service.stage_name_values.return_value = ["Prospecting"]
        service.opportunity_picklist_values.return_value = ["BidLens"]
        service.create_opportunity.return_value = "006-created"
        service.opportunity_record_url.return_value = (
            "https://example.my.salesforce.com/lightning/r/Opportunity/006-created/view"
        )

        first = self._vote_interested(service)
        second = self._vote_interested(service)

        feed_ids = {
            opp.id
            for opp, _watched in opportunities._feed_query(
                self.db,
                self.user,
                "solicitations",
            ).all()
        }
        shortlist_ids = {
            opp.id
            for opp, _watched in opportunities._my_shortlist_query(
                self.db,
                self.user,
                "solicitations",
            ).all()
        }

        third = self._vote_interested(service)

        self.assertEqual(first["salesforce_outcome"], "created")
        self.assertIsNone(second["vote"])
        self.assertNotIn(self.opp.id, feed_ids)
        self.assertNotIn(self.opp.id, shortlist_ids)
        self.assertEqual(third["salesforce_outcome"], "linked")
        service.create_opportunity.assert_called_once()
        service.update_intake_status.assert_called_once()

    def test_admin_salesforce_failure_keeps_interest_shortlisted(self):
        service = MagicMock()
        service.is_authorized.side_effect = SalesforceConfigError(
            "Salesforce is not connected"
        )

        result = self._vote_interested(service)

        self.assertTrue(result["ok"])
        self.assertEqual(result["vote"], "PURSUE")
        vote = (
            self.db.query(Vote)
            .filter(
                Vote.org_id == self.org.id,
                Vote.user_id == self.user.id,
                Vote.opp_id == self.opp.id,
            )
            .one()
        )
        self.assertEqual(vote.vote, "PURSUE")
        self.assertTrue(result["in_my_shortlist"])
        self.assertEqual(
            result["sidebar"]["my_shortlisted"][0]["id"],
            self.opp.id,
        )
        self.assertEqual(result["salesforce_outcome"], "unavailable")
        self.assertIn("remains in My Shortlist", result["salesforce_warning"])
        self.assertEqual(result["salesforce_error"], "Salesforce is not connected")

    def test_explicit_push_to_crm_still_updates_existing_match(self):
        service = MagicMock()
        service.find_opportunity_by_external_source_id.return_value = SalesforceOpportunity(
            id="006-existing",
            name="Existing Opportunity",
            external_source_id=self.opp.source_record_id,
            intake_status=None,
        )
        service.opportunity_record_url.return_value = (
            "https://example.my.salesforce.com/lightning/r/Opportunity/006-existing/view"
        )

        with (
            patch.object(api, "require_user", return_value=self.user),
            patch.object(api, "SalesforceService", return_value=service),
        ):
            result = api.api_push_opp_to_salesforce(
                self.opp.id,
                MagicMock(),
                self.db,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["salesforce_opportunity_id"], "006-existing")
        service.update_intake_status.assert_called_once()

    def test_explicit_create_in_crm_still_uses_shared_payload(self):
        service = MagicMock()
        service.is_authorized.return_value = True
        service.required_createable_opportunity_fields.return_value = []
        service.stage_name_values.return_value = ["Prospecting"]
        service.opportunity_picklist_values.return_value = ["BidLens"]
        service.create_opportunity.return_value = "006-created"
        service.opportunity_record_url.return_value = (
            "https://example.my.salesforce.com/lightning/r/Opportunity/006-created/view"
        )

        with (
            patch.object(api, "require_user", return_value=self.user),
            patch.object(api, "SalesforceService", return_value=service),
        ):
            result = api.api_create_opp_in_salesforce(
                self.opp.id,
                MagicMock(),
                self.db,
            )

        self.assertTrue(result["created"])
        self.assertEqual(result["salesforce_opportunity_id"], "006-created")
        payload = service.create_opportunity.call_args.args[0]
        self.assertEqual(payload["External_Source_ID_c__c"], "notice-1001")


if __name__ == "__main__":
    unittest.main()
