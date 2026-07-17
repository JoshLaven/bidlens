import unittest
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import (
    CompanyProfile,
    GrantsSourceConfig,
    OrgProfile,
    Organization,
    OrganizationMembership,
    SamSourceConfig,
    User,
)
from bidlens.routes.company_profile import (
    active_company_profile,
    archive_duplicate_active_profiles,
    company_profile_page,
    company_profile_save,
    upsert_company_profile,
)


class CompanyProfileTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Profile Org", slug="profile-org")
        self.db.add(self.org)
        self.db.flush()
        self.user = User(email="owner@profile.test", organization_id=self.org.id)
        self.db.add(self.user)
        self.db.flush()
        self.db.add(OrganizationMembership(
            organization_id=self.org.id,
            user_id=self.user.id,
            role="admin",
        ))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_upsert_updates_existing_active_profile(self):
        profile, created, archived_count = upsert_company_profile(
            self.db,
            org_id=self.org.id,
            website_url="https://example.com",
            uei="ORIGINALUEI12",
            cage_code="1ABC2",
            duns="123456789",
        )
        self.db.commit()

        updated, updated_created, updated_archived_count = upsert_company_profile(
            self.db,
            org_id=self.org.id,
            website_url="https://updated.example.com",
            uei="UPDATEDUEI12",
            cage_code="9XYZ8",
            duns="987654321",
        )
        self.db.commit()

        active_profiles = (
            self.db.query(CompanyProfile)
            .filter(CompanyProfile.org_id == self.org.id, CompanyProfile.archived_at.is_(None))
            .all()
        )

        self.assertTrue(created)
        self.assertFalse(updated_created)
        self.assertEqual(archived_count, 0)
        self.assertEqual(updated_archived_count, 0)
        self.assertEqual(profile.id, updated.id)
        self.assertEqual(len(active_profiles), 1)
        self.assertEqual(active_profiles[0].company_name, self.org.name)
        self.assertEqual(active_profiles[0].uei, "UPDATEDUEI12")
        self.assertEqual(active_profiles[0].cage_code, "9XYZ8")
        self.assertEqual(active_profiles[0].duns, "987654321")
        self.assertEqual(active_profiles[0].profile_json["profile_type"], "organization_identity")

    def test_duplicate_active_profiles_are_archived(self):
        older = CompanyProfile(
            org_id=self.org.id,
            company_name="Older",
            profile_json={"profile_type": "organization_identity"},
        )
        newer = CompanyProfile(
            org_id=self.org.id,
            company_name="Newer",
            profile_json={"profile_type": "organization_identity"},
        )
        self.db.add_all([older, newer])
        self.db.commit()

        archived_count = archive_duplicate_active_profiles(
            self.db,
            org_id=self.org.id,
            keep_profile_id=newer.id,
        )
        self.db.commit()

        active = active_company_profile(self.db, self.org.id)

        self.assertEqual(archived_count, 1)
        self.assertEqual(active.id, newer.id)
        self.assertIsNotNone(self.db.get(CompanyProfile, older.id).archived_at)

    def test_save_redirects_back_to_organization_overview(self):
        request = SimpleNamespace(query_params={})
        setattr(self.user, "current_organization_id", self.org.id)

        with patch("bidlens.routes.company_profile.get_current_user", return_value=self.user):
            response = asyncio.run(company_profile_save(
                request,
                website_url="https://profile.example.com",
                uei="PROFILEUEI12",
                cage_code="1PROF",
                duns="123123123",
                db=self.db,
            ))

        active = active_company_profile(self.db, self.org.id)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/company-profile?org_id={self.org.id}&saved=1")
        self.assertIsNotNone(active)
        self.assertEqual(active.website_url, "https://profile.example.com")

    def test_pre_live_admin_can_access_company_profile_setup_step(self):
        request = SimpleNamespace(query_params={}, url=SimpleNamespace(path="/company-profile"))
        setattr(self.user, "current_organization_id", self.org.id)
        setattr(self.user, "current_role", "admin")
        setattr(self.user, "current_organization_is_live", False)

        with (
            patch("bidlens.routes.company_profile.get_current_user", return_value=self.user),
            patch("bidlens.routes.company_profile.templates.TemplateResponse", return_value={"ok": True}) as template_response,
            patch("bidlens.routes.company_profile.get_sidebar", return_value={}),
        ):
            response = asyncio.run(company_profile_page(request, self.db))

        self.assertEqual(response, {"ok": True})
        template_response.assert_called_once()
        self.assertEqual(template_response.call_args.args[0], "company_profile.html")

    def test_company_profile_template_uses_compact_information_section(self):
        template = Path("src/bidlens/templates/company_profile.html").read_text()

        self.assertIn("workspace_management_hero('Organization'", template)
        self.assertIn("Company information used for opportunity matching, enrichment, and routing.", template)
        self.assertIn("Edit Organization", template)
        self.assertIn("Save Changes", template)
        self.assertIn("Cancel", template)
        self.assertIn("Workspace Users", template)
        self.assertIn("Add Users", template)
        self.assertIn("View all users", template)
        self.assertIn("Recent Awards", template)
        self.assertIn("Workspace Status", template)
        self.assertIn("connected_source_count", template)
        self.assertIn("organization-summary-metadata", template)
        self.assertIn("organization-summary-metadata--display", template)
        self.assertIn("organization-summary-metadata--edit", template)
        self.assertIn("organization-property-list", template)
        self.assertIn("organization-identity-accent", template)
        self.assertIn("Website", template)
        self.assertIn("UEI", template)
        self.assertIn("CAGE", template)
        self.assertIn("DUNS", template)
        self.assertNotIn("Workspace Profile", template)
        self.assertNotIn("Edit Information", template)
        self.assertNotIn("Company profile", template)
        self.assertNotIn("Organization details", template)
        self.assertNotIn('company-profile-eyebrow">Workspace Management', template)
        self.assertEqual(template.count("{{ profile_form.organization_name }}"), 1)

    def test_company_profile_member_preview_is_tenant_scoped(self):
        other_org = Organization(name="Other Org", slug="other-org")
        self.db.add(other_org)
        self.db.flush()
        same_org_member = User(email="same@example.com", name="Same Org", organization_id=self.org.id)
        other_org_member = User(email="other@example.com", name="Other Org", organization_id=other_org.id)
        self.db.add_all([same_org_member, other_org_member])
        self.db.flush()
        self.db.add_all([
            OrganizationMembership(organization_id=self.org.id, user_id=same_org_member.id, role="member"),
            OrganizationMembership(organization_id=other_org.id, user_id=other_org_member.id, role="admin"),
        ])
        self.db.commit()
        setattr(self.user, "current_organization_id", self.org.id)
        setattr(self.user, "current_role", "admin")
        request = SimpleNamespace(query_params={"org_id": str(self.org.id)}, url=SimpleNamespace(path="/company-profile"))

        with (
            patch("bidlens.routes.company_profile.get_current_user", return_value=self.user),
            patch("bidlens.routes.company_profile.templates.TemplateResponse", return_value={"ok": True}) as response,
            patch("bidlens.routes.company_profile.get_sidebar", return_value={}),
        ):
            asyncio.run(company_profile_page(request, self.db))

        context = response.call_args.args[1]
        emails = {row["email"] for row in context["member_preview"]}
        self.assertIn("same@example.com", emails)
        self.assertNotIn("other@example.com", emails)
        self.assertTrue(context["can_manage_members"])

    def test_company_profile_summary_context_uses_workspace_metadata(self):
        self.org.is_live = True
        self.db.add_all([
            SamSourceConfig(organization_id=self.org.id, name="Default SAM.gov Search"),
            GrantsSourceConfig(organization_id=self.org.id, enabled=True),
            OrgProfile(org_id=self.org.id, govwin_credentials_encrypted="stored"),
        ])
        second_user = User(email="second@profile.test", name="Second User", organization_id=self.org.id)
        self.db.add(second_user)
        self.db.flush()
        self.db.add(OrganizationMembership(
            organization_id=self.org.id,
            user_id=second_user.id,
            role="member",
        ))
        self.db.commit()
        setattr(self.user, "current_organization_id", self.org.id)
        setattr(self.user, "current_role", "admin")
        request = SimpleNamespace(query_params={"org_id": str(self.org.id)}, url=SimpleNamespace(path="/company-profile"))

        with (
            patch("bidlens.routes.company_profile.get_current_user", return_value=self.user),
            patch("bidlens.routes.company_profile.templates.TemplateResponse", return_value={"ok": True}) as response,
            patch("bidlens.routes.company_profile.get_sidebar", return_value={}),
        ):
            asyncio.run(company_profile_page(request, self.db))

        context = response.call_args.args[1]
        self.assertEqual(context["workspace_status"], "Live")
        self.assertEqual(context["member_count"], 2)
        self.assertEqual(context["connected_source_count"], 3)


if __name__ == "__main__":
    unittest.main()
