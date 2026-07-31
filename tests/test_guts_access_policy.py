import unittest
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import Opportunity, Organization, OrganizationMembership, User, Vote, Workspace
from bidlens.services.opportunity_knowledge_brief import (
    GUTSOpportunityNotFoundError,
    GUTSShortlistRequiredError,
    GUTSWorkspaceScopeError,
    require_guts_generation_access,
    resolve_guts_access,
)
from bidlens.services.opportunity_access import authorized_opportunity_for_user


class GutsAccessPolicyTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Access", slug="guts-access")
        self.other_org = Organization(name="Other", slug="guts-access-other")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.workspace = Workspace(organization_id=self.org.id, name="Access", slug="guts-access")
        self.other_workspace = Workspace(organization_id=self.other_org.id, name="Other", slug="guts-access-other")
        self.member = User(email="member@guts.test", organization_id=self.org.id)
        self.admin = User(email="admin@guts.test", organization_id=self.org.id)
        self.viewer = User(email="viewer@guts.test", organization_id=self.org.id)
        self.outsider = User(email="outsider@guts.test", organization_id=self.other_org.id)
        self.db.add_all([self.workspace, self.other_workspace, self.member, self.admin, self.viewer, self.outsider])
        self.db.flush()
        self.db.add_all([
            OrganizationMembership(organization_id=self.org.id, user_id=self.member.id, role="member"),
            OrganizationMembership(organization_id=self.org.id, user_id=self.admin.id, role="admin"),
            OrganizationMembership(organization_id=self.org.id, user_id=self.viewer.id, role="member"),
            OrganizationMembership(organization_id=self.other_org.id, user_id=self.outsider.id, role="member"),
        ])
        self.opportunity = Opportunity(
            organization_id=self.org.id, source="test", source_record_id="ACCESS-1",
            title="Access", agency="Agency", opportunity_type="RFP",
            posted_date=date(2026, 7, 31), response_deadline=date(2026, 9, 1),
            qualification_status="qualified",
        )
        self.unqualified = Opportunity(
            organization_id=self.org.id, source="test", source_record_id="ACCESS-2",
            title="Admin only", agency="Agency", opportunity_type="RFP",
            posted_date=date(2026, 7, 31), response_deadline=date(2026, 9, 1),
            qualification_status="unreviewed",
        )
        self.db.add_all([self.opportunity, self.unqualified])
        self.db.commit()
        for user, role, org_id in (
            (self.member, "member", self.org.id),
            (self.admin, "admin", self.org.id),
            (self.viewer, "member", self.org.id),
            (self.outsider, "member", self.other_org.id),
        ):
            user.current_role = role
            user.current_organization_id = org_id

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _vote(self, user, value):
        self.db.add(Vote(org_id=self.org.id, opp_id=self.opportunity.id, user_id=user.id, vote=value))
        self.db.commit()

    def test_member_with_current_pursue_may_generate(self):
        self._vote(self.member, "PURSUE")
        context = require_guts_generation_access(
            self.db, user=self.member, opportunity_id=self.opportunity.id,
        )
        self.assertTrue(context.may_view)
        self.assertTrue(context.may_generate)
        self.assertEqual(context.workspace_id, self.workspace.id)

    def test_member_without_vote_or_with_pass_is_rejected(self):
        with self.assertRaises(GUTSShortlistRequiredError):
            require_guts_generation_access(self.db, user=self.member, opportunity_id=self.opportunity.id)
        self._vote(self.member, "PASS")
        with self.assertRaises(GUTSShortlistRequiredError):
            require_guts_generation_access(self.db, user=self.member, opportunity_id=self.opportunity.id)

    def test_admin_has_no_shortlist_bypass_but_pursue_allows_generation(self):
        with self.assertRaises(GUTSShortlistRequiredError):
            require_guts_generation_access(self.db, user=self.admin, opportunity_id=self.opportunity.id)
        self._vote(self.admin, "PURSUE")
        self.assertTrue(require_guts_generation_access(
            self.db, user=self.admin, opportunity_id=self.opportunity.id,
        ).may_generate)

    def test_authorized_non_shortlisting_viewer_may_view_shared_briefing(self):
        context = resolve_guts_access(self.db, user=self.viewer, opportunity_id=self.opportunity.id)
        self.assertTrue(context.may_view)
        self.assertFalse(context.may_generate)

    def test_cross_organization_and_workspace_mismatch_are_rejected(self):
        with self.assertRaises(GUTSOpportunityNotFoundError):
            resolve_guts_access(self.db, user=self.outsider, opportunity_id=self.opportunity.id)
        with self.assertRaises(GUTSWorkspaceScopeError):
            resolve_guts_access(
                self.db, user=self.member, opportunity_id=self.opportunity.id,
                expected_workspace_id=self.other_workspace.id,
            )

    def test_normal_opportunity_folder_authorization_is_preserved(self):
        self.assertIsNotNone(authorized_opportunity_for_user(
            self.db, user=self.member, opportunity_id=self.opportunity.id,
        ))
        self.assertIsNone(authorized_opportunity_for_user(
            self.db, user=self.member, opportunity_id=self.unqualified.id,
        ))
        self.assertIsNotNone(authorized_opportunity_for_user(
            self.db, user=self.admin, opportunity_id=self.unqualified.id,
        ))
        admin_context = resolve_guts_access(self.db, user=self.admin, opportunity_id=self.unqualified.id)
        self.assertTrue(admin_context.may_view)
        self.assertFalse(admin_context.may_generate)


if __name__ == "__main__":
    unittest.main()
