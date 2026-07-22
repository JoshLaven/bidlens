import datetime as dt
import re
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import Event, Opportunity, OpportunityOutcome, Organization, OrganizationMembership, User, Vote
from bidlens.services.opportunity_outcomes import (
    OUTCOME_BIDDING,
    OUTCOME_NO_BID,
    past_due_outcome_workflow_visible_exists,
    record_opportunity_outcome,
    unresolved_past_due_outcome_count,
    unresolved_past_due_outcomes,
)


TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "bidlens" / "templates"


class PastDueOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

        self.org = Organization(name="Outcome Org", slug="outcome-org")
        self.other_org = Organization(name="Other Org", slug="other-outcome-org")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.admin = User(email="admin@outcome.test", organization_id=self.org.id)
        self.member = User(email="member@outcome.test", organization_id=self.org.id)
        self.other_admin = User(email="admin@other.test", organization_id=self.other_org.id)
        self.db.add_all([self.admin, self.member, self.other_admin])
        self.db.flush()
        self.db.add_all([
            OrganizationMembership(organization_id=self.org.id, user_id=self.admin.id, role="admin"),
            OrganizationMembership(organization_id=self.org.id, user_id=self.member.id, role="member"),
            OrganizationMembership(organization_id=self.other_org.id, user_id=self.other_admin.id, role="admin"),
        ])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _opportunity(self, **overrides):
        values = {
            "organization_id": self.org.id,
            "source": "sam",
            "source_record_id": f"OUTCOME-{len(overrides)}-{dt.datetime.now(dt.timezone.utc).timestamp()}",
            "title": "Past due opportunity",
            "agency": "Department of Testing",
            "opportunity_type": "Solicitation",
            "posted_date": dt.date.today() - dt.timedelta(days=20),
            "response_deadline": dt.date.today() - dt.timedelta(days=1),
            "qualification_status": "qualified",
            "decision_state": "INBOX",
        }
        values.update(overrides)
        opportunity = Opportunity(**values)
        self.db.add(opportunity)
        self.db.commit()
        return opportunity

    def _pursue_before_deadline(self, opportunity, user=None):
        user = user or self.member
        vote = Vote(
            org_id=opportunity.organization_id,
            opp_id=opportunity.id,
            user_id=user.id,
            vote="PURSUE",
            updated_at=dt.datetime.combine(
                opportunity.response_deadline,
                dt.time(12, 0),
                tzinfo=dt.timezone.utc,
            ),
        )
        self.db.add(vote)
        self.db.commit()
        return vote

    def test_qualified_past_due_previously_pursued_opportunity_is_eligible(self):
        opportunity = self._opportunity()
        self._pursue_before_deadline(opportunity)

        results = unresolved_past_due_outcomes(self.db, organization_id=self.org.id)

        self.assertEqual([opp.id for opp in results], [opportunity.id])
        self.assertEqual(unresolved_past_due_outcome_count(self.db, organization_id=self.org.id), 1)

    def test_future_due_opportunity_is_not_eligible(self):
        opportunity = self._opportunity(response_deadline=dt.date.today() + dt.timedelta(days=2))
        self._pursue_before_deadline(opportunity)

        self.assertEqual(unresolved_past_due_outcomes(self.db, organization_id=self.org.id), [])

    def test_never_pursued_opportunity_is_not_eligible(self):
        self._opportunity()

        self.assertEqual(unresolved_past_due_outcomes(self.db, organization_id=self.org.id), [])

    def test_rejected_opportunity_is_not_eligible(self):
        opportunity = self._opportunity(qualification_status="rejected")
        self._pursue_before_deadline(opportunity)

        self.assertEqual(unresolved_past_due_outcomes(self.db, organization_id=self.org.id), [])

    def test_resolved_opportunity_is_not_eligible(self):
        opportunity = self._opportunity()
        self._pursue_before_deadline(opportunity)
        record_opportunity_outcome(
            self.db,
            organization_id=self.org.id,
            opportunity_id=opportunity.id,
            outcome_type=OUTCOME_BIDDING,
            recorded_by=self.admin.id,
        )

        self.assertEqual(unresolved_past_due_outcomes(self.db, organization_id=self.org.id), [])

    def test_other_workspace_opportunity_is_not_eligible(self):
        opportunity = self._opportunity(organization_id=self.other_org.id)
        self._pursue_before_deadline(opportunity, user=self.other_admin)

        self.assertEqual(unresolved_past_due_outcomes(self.db, organization_id=self.org.id), [])

    def test_pursue_after_deadline_is_not_eligible(self):
        opportunity = self._opportunity()
        self.db.add(Vote(
            org_id=self.org.id,
            opp_id=opportunity.id,
            user_id=self.member.id,
            vote="PURSUE",
            updated_at=dt.datetime.combine(
                dt.date.today(),
                dt.time(12, 0),
                tzinfo=dt.timezone.utc,
            ),
        ))
        self.db.commit()

        self.assertEqual(unresolved_past_due_outcomes(self.db, organization_id=self.org.id), [])

    def test_past_due_eligible_opportunity_is_excluded_from_active_my_shortlist(self):
        past_due = self._opportunity(source_record_id="PAST")
        future_due = self._opportunity(
            source_record_id="FUTURE",
            response_deadline=dt.date.today() + dt.timedelta(days=10),
        )
        self._pursue_before_deadline(past_due, user=self.admin)
        self._pursue_before_deadline(future_due, user=self.admin)

        active_shortlist_ids = {
            opp_id
            for (opp_id,) in (
                self.db.query(Opportunity.id)
                .join(
                    Vote,
                    (Vote.opp_id == Opportunity.id)
                    & (Vote.org_id == self.org.id)
                    & (Vote.user_id == self.admin.id)
                    & (Vote.vote == "PURSUE"),
                )
                .filter(Opportunity.organization_id == self.org.id)
                .filter(Opportunity.qualification_status == "qualified")
                .filter(Opportunity.decision_state != "ARCHIVED")
                .filter(~past_due_outcome_workflow_visible_exists(organization_id=self.org.id))
                .all()
            )
        }

        self.assertNotIn(past_due.id, active_shortlist_ids)
        self.assertIn(future_due.id, active_shortlist_ids)
        self.assertEqual(
            [opp.id for opp in unresolved_past_due_outcomes(self.db, organization_id=self.org.id)],
            [past_due.id],
        )
        self.assertEqual(
            self.db.query(Vote).filter(Vote.opp_id == past_due.id, Vote.vote == "PURSUE").count(),
            1,
        )
        self.assertEqual(
            self.db.query(Vote).filter(Vote.opp_id == past_due.id, Vote.vote == "PASS").count(),
            0,
        )

    def test_past_due_ineligible_opportunity_remains_accessible_in_my_shortlist(self):
        opportunity = self._opportunity(source_record_id="PAST-INELIGIBLE")
        self.db.add(Vote(
            org_id=self.org.id,
            opp_id=opportunity.id,
            user_id=self.admin.id,
            vote="PURSUE",
            updated_at=dt.datetime.combine(
                dt.date.today(),
                dt.time(12, 0),
                tzinfo=dt.timezone.utc,
            ),
        ))
        self.db.commit()

        active_shortlist_ids = {
            opp_id
            for (opp_id,) in (
                self.db.query(Opportunity.id)
                .join(
                    Vote,
                    (Vote.opp_id == Opportunity.id)
                    & (Vote.org_id == self.org.id)
                    & (Vote.user_id == self.admin.id)
                    & (Vote.vote == "PURSUE"),
                )
                .filter(Opportunity.organization_id == self.org.id)
                .filter(Opportunity.qualification_status == "qualified")
                .filter(Opportunity.decision_state != "ARCHIVED")
                .filter(~past_due_outcome_workflow_visible_exists(organization_id=self.org.id))
                .all()
            )
        }

        self.assertIn(opportunity.id, active_shortlist_ids)
        self.assertEqual(unresolved_past_due_outcomes(self.db, organization_id=self.org.id), [])

    def test_recorded_past_due_outcome_does_not_return_to_active_my_shortlist(self):
        opportunity = self._opportunity(source_record_id="PAST-RESOLVED")
        self._pursue_before_deadline(opportunity, user=self.admin)
        record_opportunity_outcome(
            self.db,
            organization_id=self.org.id,
            opportunity_id=opportunity.id,
            outcome_type=OUTCOME_BIDDING,
            recorded_by=self.member.id,
        )

        active_shortlist_ids = {
            opp_id
            for (opp_id,) in (
                self.db.query(Opportunity.id)
                .join(
                    Vote,
                    (Vote.opp_id == Opportunity.id)
                    & (Vote.org_id == self.org.id)
                    & (Vote.user_id == self.admin.id)
                    & (Vote.vote == "PURSUE"),
                )
                .filter(Opportunity.organization_id == self.org.id)
                .filter(Opportunity.qualification_status == "qualified")
                .filter(Opportunity.decision_state != "ARCHIVED")
                .filter(~past_due_outcome_workflow_visible_exists(organization_id=self.org.id))
                .all()
            )
        }

        self.assertNotIn(opportunity.id, active_shortlist_ids)
        self.assertEqual(unresolved_past_due_outcomes(self.db, organization_id=self.org.id), [])

    def test_workspace_member_can_record_bidding_and_no_bid_without_duplicate_rows(self):
        opportunity = self._opportunity()
        self._pursue_before_deadline(opportunity)

        first = record_opportunity_outcome(
            self.db,
            organization_id=self.org.id,
            opportunity_id=opportunity.id,
            outcome_type=OUTCOME_BIDDING,
            recorded_by=self.member.id,
        )
        second = record_opportunity_outcome(
            self.db,
            organization_id=self.org.id,
            opportunity_id=opportunity.id,
            outcome_type=OUTCOME_NO_BID,
            recorded_by=self.member.id,
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(second.outcome_type, OUTCOME_NO_BID)
        self.assertEqual(second.recorded_by, self.member.id)
        self.assertIsNotNone(second.recorded_at)
        self.assertIsNone(second.reason_code)
        self.assertIsNone(second.reason_text)
        self.assertIsNone(second.notes)
        self.assertEqual(
            self.db.query(OpportunityOutcome)
            .filter_by(organization_id=self.org.id, opportunity_id=opportunity.id)
            .count(),
            1,
        )
        self.assertEqual(
            self.db.query(Event)
            .filter_by(org_id=self.org.id, opp_id=opportunity.id, event_type="opportunity_outcome_recorded")
            .count(),
            2,
        )

    def test_outcome_cannot_be_recorded_for_ineligible_future_opportunity(self):
        opportunity = self._opportunity(response_deadline=dt.date.today() + dt.timedelta(days=10))
        self._pursue_before_deadline(opportunity)

        with self.assertRaises(ValueError):
            record_opportunity_outcome(
                self.db,
                organization_id=self.org.id,
                opportunity_id=opportunity.id,
                outcome_type=OUTCOME_BIDDING,
                recorded_by=self.admin.id,
            )

        self.assertEqual(self.db.query(OpportunityOutcome).count(), 0)


class PastDueOutcomeTemplateTests(unittest.TestCase):
    def test_routes_allow_workspace_users_and_return_remaining_count(self):
        routes = (Path(__file__).resolve().parents[1] / "src" / "bidlens" / "routes" / "opportunities.py").read_text()
        match = re.search(
            r'@router\.get\("/past-due-outcomes"\)[\s\S]+?@router\.get\("/opportunities/export\.csv"\)',
            routes,
        )

        self.assertIsNotNone(match)
        route_block = match.group(0)
        self.assertIn('@router.get("/past-due-outcomes")', routes)
        self.assertIn('@router.post("/past-due-outcomes/{opp_id}")', routes)
        self.assertNotIn("if not _is_admin(user):", route_block)
        self.assertNotIn("Workspace Admin access required", route_block)
        self.assertIn('"remaining_count": remaining_count', route_block)
        self.assertIn("recorded_by=user.id", route_block)
        self.assertIn("resolved_outcomes", route_block)
        self.assertNotIn("transitionOpp('{{ item.opportunity.id }}','NO_BID')", routes)

    def test_my_shortlist_query_uses_shared_past_due_exclusion(self):
        routes = (Path(__file__).resolve().parents[1] / "src" / "bidlens" / "routes" / "opportunities.py").read_text()
        match = re.search(
            r"def _my_shortlist_query[\s\S]+?past_due_outcome_workflow_visible_exists",
            routes,
        )

        self.assertIsNotNone(match)

    def test_auth_sets_notification_count_for_workspace_members(self):
        auth = (Path(__file__).resolve().parents[1] / "src" / "bidlens" / "auth.py").read_text()

        self.assertIn("past_due_outcome_count", auth)
        self.assertIn("if membership", auth)
        self.assertNotIn('if (membership and membership.role == "admin")', auth)

    def test_base_notification_is_available_to_workspace_users_and_uses_count(self):
        base = (TEMPLATES / "base.html").read_text()
        match = re.search(r"{% if user and[\s\S]+?data-past-due-outcome-notification", base)

        self.assertIsNotNone(match)
        notification_block = match.group(0)
        self.assertIn("data-past-due-outcome-notification", base)
        self.assertIn("show_past_due_notification", base)
        self.assertIn("request_path == '/'", base)
        self.assertIn("request_path.startswith('/my-shortlist')", base)
        self.assertIn("request_path.startswith('/archive')", base)
        visibility_block = re.search(
            r"{% set show_past_due_notification[\s\S]+?%}",
            base,
        ).group(0)
        self.assertNotIn("request_path.startswith('/triage')", visibility_block)
        self.assertNotIn("request_path.startswith('/home')", visibility_block)
        self.assertIn("user.past_due_outcome_count > 0", base)
        self.assertNotIn("is_workspace_admin", notification_block)
        self.assertIn("View Past Due", base)

    def test_review_interface_is_compact_and_excludes_standard_card_controls(self):
        template = (TEMPLATES / "past_due_outcomes.html").read_text()

        self.assertIn("Are you bidding this?", template)
        self.assertIn("We’re Bidding", template)
        self.assertIn("No Bid", template)
        self.assertIn("These opportunities have passed their response deadline while on your Shortlist.", template)
        self.assertIn("Close the loop and clear them from your queue.", template)
        self.assertIn("Completed Reviews ({{ resolved_outcomes|length }})", template)
        self.assertIn("<details class=\"past-due-resolved accordion\">", template)
        self.assertIn("No completed reviews yet.", template)
        self.assertNotIn("Previously recorded outcomes", template)
        self.assertNotIn("RECORDED OUTCOMES", template)
        self.assertIn("Recorded {{ outcome.recorded_at.strftime('%b') }}", template)
        self.assertIn("by {{ recorder.email }}", template)
        self.assertIn('href="/opportunity/{{ opp.id }}"', template)
        self.assertNotIn("More Info", template)
        self.assertNotIn("opp_card", template)
        self.assertNotIn("showArchiveModal", template)
        self.assertNotIn("reason_code", template)
        self.assertNotIn("reason_text", template)
        self.assertNotIn("data-feed-archive-checkbox", template)
        self.assertNotIn("data-team-interest", template)


if __name__ == "__main__":
    unittest.main()
