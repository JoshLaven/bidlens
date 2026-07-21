import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import (
    Opportunity,
    OpportunityHistoryEvent,
    Organization,
    OrganizationMembership,
    User,
    Vote,
)
from bidlens.routes import api, opportunities
from bidlens.services.salesforce import (
    SalesforceConfigError,
    SalesforceOpportunity,
)
from fastapi import HTTPException


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

    def _vote_interested(self, service, *, user=None):
        with (
            patch.object(api, "require_user", return_value=user or self.user),
            patch.object(api, "SalesforceService", return_value=service),
        ):
            return api.api_vote(
                api.VoteIn(opp_id=self.opp.id, vote="PURSUE"),
                MagicMock(),
                self.db,
            )

    def test_member_interest_creates_salesforce_opportunity(self):
        service = MagicMock()
        service.is_authorized.return_value = True
        service.find_opportunity_by_external_source_id.return_value = None
        service.required_createable_opportunity_fields.return_value = []
        service.stage_name_values.return_value = ["Prospecting"]
        service.opportunity_picklist_values.return_value = ["SAM", "Grants.gov", "GovWin"]
        service.create_opportunity.return_value = "006-member-created"
        service.opportunity_record_url.return_value = (
            "https://example.my.salesforce.com/lightning/r/Opportunity/006-member-created/view"
        )

        result = self._vote_interested(service, user=self.member)
        self.db.refresh(self.opp)

        self.assertEqual(result["vote"], "PURSUE")
        self.assertEqual(result["salesforce_outcome"], "created")
        self.assertFalse(result["admin_crm_action"])
        self.assertEqual(self.opp.salesforce_opportunity_id, "006-member-created")
        service.create_opportunity.assert_called_once()

    def test_member_interest_links_existing_salesforce_opportunity(self):
        service = MagicMock()
        service.is_authorized.return_value = True
        service.find_opportunity_by_external_source_id.return_value = SalesforceOpportunity(
            id="006-member-existing",
            name="Existing Opportunity",
            external_source_id=self.opp.source_record_id,
            intake_status=None,
        )
        service.opportunity_record_url.return_value = (
            "https://example.my.salesforce.com/lightning/r/Opportunity/006-member-existing/view"
        )

        result = self._vote_interested(service, user=self.member)
        self.db.refresh(self.opp)

        self.assertEqual(result["vote"], "PURSUE")
        self.assertEqual(result["salesforce_outcome"], "matched_existing")
        self.assertEqual(self.opp.salesforce_opportunity_id, "006-member-existing")
        service.create_opportunity.assert_not_called()

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

    def _salesforce_history_actions(self):
        rows = (
            self.db.query(OpportunityHistoryEvent)
            .filter(
                OpportunityHistoryEvent.opportunity_id == self.opp.id,
                OpportunityHistoryEvent.event_type == "salesforce_synchronized",
            )
            .order_by(OpportunityHistoryEvent.id.asc())
            .all()
        )
        return [row.event_data["action"] for row in rows]

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
        self.assertEqual(result["salesforce_outcome"], "matched_existing")
        self.assertEqual(self.opp.salesforce_opportunity_id, "006-existing")
        self.assertEqual(
            result["sidebar"]["my_shortlisted"][0]["salesforce_opportunity_url"],
            "https://example.my.salesforce.com/lightning/r/Opportunity/006-existing/view",
        )
        service.update_intake_status.assert_called_once()
        service.create_opportunity.assert_not_called()
        self.assertEqual(self._salesforce_history_actions(), ["matched_existing"])

    def test_interested_with_local_salesforce_id_does_not_create_again(self):
        self.opp.salesforce_opportunity_id = "006-local"
        self.opp.salesforce_opportunity_url = (
            "https://example.my.salesforce.com/lightning/r/Opportunity/006-local/view"
        )
        self.db.commit()
        service = MagicMock()
        service.is_authorized.return_value = True

        result = self._vote_interested(service, user=self.member)

        self.assertEqual(result["salesforce_outcome"], "already_linked")
        service.find_opportunity_by_external_source_id.assert_not_called()
        service.create_opportunity.assert_not_called()
        service.update_intake_status.assert_called_once_with("006-local", "Prospect_Feed")

    def test_interested_creates_when_no_match_exists(self):
        service = MagicMock()
        service.is_authorized.return_value = True
        service.find_opportunity_by_external_source_id.return_value = None
        service.required_createable_opportunity_fields.return_value = []
        service.stage_name_values.return_value = ["Prospecting"]
        service.opportunity_picklist_values.return_value = ["SAM", "Grants.gov", "GovWin"]
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
        self.assertEqual(payload["External_Source_ID__c"], "notice-1001")
        self.assertEqual(payload["Intake_Source__c"], "SAM")
        self.assertEqual(self._salesforce_history_actions(), ["created"])

    def test_interest_is_user_scoped_while_salesforce_link_is_shared(self):
        service = MagicMock()
        service.is_authorized.return_value = True
        service.find_opportunity_by_external_source_id.return_value = None
        service.required_createable_opportunity_fields.return_value = []
        service.stage_name_values.return_value = ["Prospecting"]
        service.opportunity_picklist_values.return_value = ["SAM", "Grants.gov", "GovWin"]
        service.create_opportunity.return_value = "006-shared-link"
        service.opportunity_record_url.return_value = (
            "https://example.my.salesforce.com/lightning/r/Opportunity/006-shared-link/view"
        )

        first = self._vote_interested(service, user=self.user)
        self.db.refresh(self.opp)

        user_a_feed_ids = {
            opp.id
            for opp, _watched in opportunities._feed_query(
                self.db,
                self.user,
                "solicitations",
            ).all()
        }
        user_a_shortlist_ids = {
            opp.id
            for opp, _watched in opportunities._my_shortlist_query(
                self.db,
                self.user,
                "solicitations",
            ).all()
        }
        user_b_feed_ids = {
            opp.id
            for opp, _watched in opportunities._feed_query(
                self.db,
                self.member,
                "solicitations",
            ).all()
        }
        user_b_shortlist_ids = {
            opp.id
            for opp, _watched in opportunities._my_shortlist_query(
                self.db,
                self.member,
                "solicitations",
            ).all()
        }

        self.assertEqual(first["salesforce_outcome"], "created")
        self.assertEqual(self.opp.salesforce_opportunity_id, "006-shared-link")
        self.assertNotIn(self.opp.id, user_a_feed_ids)
        self.assertIn(self.opp.id, user_a_shortlist_ids)
        self.assertIn(self.opp.id, user_b_feed_ids)
        self.assertNotIn(self.opp.id, user_b_shortlist_ids)

        second = self._vote_interested(service, user=self.member)
        self.db.refresh(self.opp)

        user_b_feed_ids = {
            opp.id
            for opp, _watched in opportunities._feed_query(
                self.db,
                self.member,
                "solicitations",
            ).all()
        }
        user_b_shortlist_ids = {
            opp.id
            for opp, _watched in opportunities._my_shortlist_query(
                self.db,
                self.member,
                "solicitations",
            ).all()
        }

        self.assertEqual(second["salesforce_outcome"], "already_linked")
        self.assertEqual(self.opp.salesforce_opportunity_id, "006-shared-link")
        self.assertNotIn(self.opp.id, user_b_feed_ids)
        self.assertIn(self.opp.id, user_b_shortlist_ids)
        service.create_opportunity.assert_called_once()
        service.update_intake_status.assert_called_once_with(
            "006-shared-link",
            "Prospect_Feed",
        )

    def test_repeated_interest_does_not_create_duplicate_and_toggle_reenters_feed(self):
        service = MagicMock()
        service.is_authorized.return_value = True
        service.find_opportunity_by_external_source_id.return_value = None
        service.required_createable_opportunity_fields.return_value = []
        service.stage_name_values.return_value = ["Prospecting"]
        service.opportunity_picklist_values.return_value = ["SAM", "Grants.gov", "GovWin"]
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
        self.assertIn(self.opp.id, feed_ids)
        self.assertNotIn(self.opp.id, shortlist_ids)
        self.assertEqual(third["salesforce_outcome"], "already_linked")
        service.create_opportunity.assert_called_once()
        service.update_intake_status.assert_called_once()

    def test_member_salesforce_failure_keeps_interest_shortlisted(self):
        service = MagicMock()
        service.is_authorized.side_effect = SalesforceConfigError(
            "Salesforce is not connected"
        )

        result = self._vote_interested(service, user=self.member)

        self.assertTrue(result["ok"])
        self.assertEqual(result["vote"], "PURSUE")
        vote = (
            self.db.query(Vote)
            .filter(
                Vote.org_id == self.org.id,
                Vote.user_id == self.member.id,
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
        self.assertEqual(result["salesforce_outcome"], "not_configured")
        self.assertIn("remains in My Shortlist", result["salesforce_warning"])
        self.assertEqual(result["salesforce_error"], "Salesforce sync could not be completed.")
        history = (
            self.db.query(OpportunityHistoryEvent)
            .filter(
                OpportunityHistoryEvent.opportunity_id == self.opp.id,
                OpportunityHistoryEvent.event_type == "salesforce_synchronized",
            )
            .one()
        )
        self.assertEqual(history.event_data["action"], "failed")

    def test_admin_interested_continues_to_create_salesforce_opportunity(self):
        service = MagicMock()
        service.is_authorized.return_value = True
        service.find_opportunity_by_external_source_id.return_value = None
        service.required_createable_opportunity_fields.return_value = []
        service.stage_name_values.return_value = ["Prospecting"]
        service.opportunity_picklist_values.return_value = ["SAM", "Grants.gov", "GovWin"]
        service.create_opportunity.return_value = "006-admin-created"
        service.opportunity_record_url.return_value = (
            "https://example.my.salesforce.com/lightning/r/Opportunity/006-admin-created/view"
        )

        result = self._vote_interested(service)

        self.assertEqual(result["salesforce_outcome"], "created")
        self.assertEqual(result["vote"], "PURSUE")

    def test_removing_interested_does_not_invoke_salesforce_creation(self):
        existing_vote = Vote(
            org_id=self.org.id,
            user_id=self.member.id,
            opp_id=self.opp.id,
            vote="PURSUE",
        )
        self.db.add(existing_vote)
        self.db.commit()
        service = MagicMock()

        result = self._vote_interested(service, user=self.member)

        self.assertIsNone(result["vote"])
        self.assertEqual(result["salesforce_outcome"], "not_requested")
        service.create_opportunity.assert_not_called()
        service.find_opportunity_by_external_source_id.assert_not_called()

    def test_explicit_push_to_crm_still_updates_existing_match(self):
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

        with (
            patch.object(api, "require_admin", return_value=self.user),
            patch.object(api, "SalesforceService", return_value=service),
        ):
            result = api.api_push_opp_to_salesforce(
                self.opp.id,
                MagicMock(),
                self.db,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["salesforce_opportunity_id"], "006-existing")
        self.assertEqual(result["salesforce_outcome"], "matched_existing")
        service.update_intake_status.assert_called_once()

    def test_explicit_create_in_crm_uses_lookup_before_create(self):
        service = MagicMock()
        service.is_authorized.return_value = True
        service.find_opportunity_by_external_source_id.return_value = SalesforceOpportunity(
            id="006-existing-create",
            name="Existing Opportunity",
            external_source_id=self.opp.source_record_id,
            intake_status=None,
        )
        service.opportunity_record_url.return_value = (
            "https://example.my.salesforce.com/lightning/r/Opportunity/006-existing-create/view"
        )

        with (
            patch.object(api, "require_admin", return_value=self.user),
            patch.object(api, "SalesforceService", return_value=service),
        ):
            result = api.api_create_opp_in_salesforce(
                self.opp.id,
                MagicMock(),
                self.db,
            )

        self.assertFalse(result["created"])
        self.assertEqual(result["salesforce_outcome"], "matched_existing")
        self.assertEqual(result["salesforce_opportunity_id"], "006-existing-create")
        service.create_opportunity.assert_not_called()

    def test_explicit_create_in_crm_creates_when_no_match_exists(self):
        service = MagicMock()
        service.is_authorized.return_value = True
        service.find_opportunity_by_external_source_id.return_value = None
        service.required_createable_opportunity_fields.return_value = []
        service.stage_name_values.return_value = ["Prospecting"]
        service.opportunity_picklist_values.return_value = ["SAM", "Grants.gov", "GovWin"]
        service.create_opportunity.return_value = "006-created"
        service.opportunity_record_url.return_value = (
            "https://example.my.salesforce.com/lightning/r/Opportunity/006-created/view"
        )

        with (
            patch.object(api, "require_admin", return_value=self.user),
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
        self.assertEqual(payload["External_Source_ID__c"], "notice-1001")
        self.assertEqual(payload["Intake_Source__c"], "SAM")

    def test_explicit_crm_routes_reject_non_admin_direct_calls(self):
        with patch.object(api, "require_user", return_value=self.member):
            with self.assertRaises(HTTPException) as push_ctx:
                api.api_push_opp_to_salesforce(self.opp.id, MagicMock(), self.db)
            with self.assertRaises(HTTPException) as create_ctx:
                api.api_create_opp_in_salesforce(self.opp.id, MagicMock(), self.db)

        self.assertEqual(push_ctx.exception.status_code, 403)
        self.assertEqual(create_ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
