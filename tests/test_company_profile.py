import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from bidlens.database import Base
from bidlens.models import (
    CompanyProfile,
    ExternalIntegrationConnection,
    GrantsSourceConfig,
    Organization,
    OrganizationMembership,
    OrganizationResource,
    OrgProfile,
    SamSourceConfig,
    SalesforceConnection,
    User,
    Workspace,
)
from bidlens.routes.company_profile import (
    active_company_profile,
    archive_duplicate_active_profiles,
    company_profile_page,
    company_profile_save,
    organization_resource_create,
    organization_resource_delete,
    organization_resource_update,
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
        self.user = User(email="owner@profile.test", name="Profile Owner", organization_id=self.org.id)
        self.db.add(self.user)
        self.db.flush()
        self.db.add(OrganizationMembership(
            organization_id=self.org.id,
            user_id=self.user.id,
            role="admin",
        ))
        self.db.commit()
        self.user.current_organization_id = self.org.id
        self.user.current_role = "admin"

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _request(self, **query):
        return SimpleNamespace(
            query_params={key: str(value) for key, value in query.items()},
            url=SimpleNamespace(path="/company-profile"),
        )

    def _render_context(self, user=None, **query):
        user = user or self.user
        with (
            patch("bidlens.routes.company_profile.get_current_user", return_value=user),
            patch("bidlens.routes.company_profile.templates.TemplateResponse", return_value={"ok": True}) as response,
            patch("bidlens.routes.company_profile.get_sidebar", return_value={}),
        ):
            asyncio.run(company_profile_page(self._request(**query), self.db))
        return response.call_args.args[1]

    def test_upsert_updates_existing_active_profile(self):
        profile, created, _ = upsert_company_profile(
            self.db,
            org_id=self.org.id,
            website_url="https://example.com",
            uei="ORIGINALUEI12",
            cage_code="1ABC2",
            duns="123456789",
        )
        self.db.commit()
        updated, updated_created, _ = upsert_company_profile(
            self.db,
            org_id=self.org.id,
            website_url="https://updated.example.com",
            uei="UPDATEDUEI12",
            cage_code="9XYZ8",
            duns="987654321",
        )
        self.db.commit()

        self.assertTrue(created)
        self.assertFalse(updated_created)
        self.assertEqual(profile.id, updated.id)
        self.assertEqual(updated.company_name, self.org.name)
        self.assertEqual(updated.uei, "UPDATEDUEI12")

    def test_duplicate_active_profiles_are_archived(self):
        older = CompanyProfile(org_id=self.org.id, company_name="Older", profile_json={})
        newer = CompanyProfile(org_id=self.org.id, company_name="Newer", profile_json={})
        self.db.add_all([older, newer])
        self.db.commit()
        count = archive_duplicate_active_profiles(self.db, org_id=self.org.id, keep_profile_id=newer.id)
        self.db.commit()
        self.assertEqual(count, 1)
        self.assertEqual(active_company_profile(self.db, self.org.id).id, newer.id)
        self.assertIsNotNone(self.db.get(CompanyProfile, older.id).archived_at)

    def test_admin_save_redirects_to_profile(self):
        with patch("bidlens.routes.company_profile.get_current_user", return_value=self.user):
            response = asyncio.run(company_profile_save(
                self._request(),
                website_url="https://profile.example.com",
                uei="PROFILEUEI12",
                cage_code="1PROF",
                duns="123123123",
                db=self.db,
            ))
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/company-profile?org_id={self.org.id}&saved=1")

    def test_pre_live_admin_can_access_company_profile(self):
        self.user.current_organization_is_live = False
        context = self._render_context()
        self.assertEqual(context["organization"].id, self.org.id)

    def test_company_profile_route_renders_real_template(self):
        self.user.current_organization_is_live = True
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/company-profile",
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
        })
        with (
            patch("bidlens.routes.company_profile.get_current_user", return_value=self.user),
            patch("bidlens.routes.company_profile.get_sidebar", return_value={}),
        ):
            response = asyncio.run(company_profile_page(request, self.db))
        html = response.body.decode("utf-8")
        self.assertIn("Profile Org", html)
        self.assertIn("Organization Profile", html)
        self.assertIn("Pinned resources and notes for your team.", html)
        self.assertIn("organization-resource-create-dialog", html)
        self.assertNotIn("Manage Resources", html)
        self.assertNotIn("Recent Awards", html)

    def test_template_uses_profile_resources_and_removes_legacy_sections(self):
        template = Path("src/bidlens/templates/company_profile.html").read_text()
        for expected in (
            "workspace-management-hero organization-profile-hero",
            "Organization Profile",
            "Legal Name",
            "Workspace Owner",
            "Opportunity Sources",
            "Integrations",
            "Federal Identifiers",
            "Pinned resources and notes for your team.",
            "organization-resource-create-dialog",
            "organization-resource-menu",
            "organization-resource-main",
        ):
            self.assertIn(expected, template)
        for removed in ("Workspace Users", "Recent Awards", "organization-updated", "connected_source_count"):
            self.assertNotIn(removed, template)
        self.assertNotIn("Manage Resources", template)
        self.assertNotIn(">Read<", template)

    def test_resource_actions_use_portal_menu_and_shared_centered_dialogs(self):
        template = Path("src/bidlens/templates/company_profile.html").read_text()
        styles = Path("src/bidlens/static/css/styles.css").read_text()

        self.assertIn('class="organization-resource-menu__trigger"', template)
        self.assertIn('role="menu"', template)
        self.assertIn('role="menuitem"', template)
        self.assertIn("document.body.appendChild(popover)", template)
        self.assertIn("trigger.getBoundingClientRect()", template)
        self.assertIn("roomBelow", template)
        self.assertIn("event.key === 'Escape'", template)
        self.assertIn("organization-resource-create-dialog", template)
        self.assertIn("organization-resource-edit-{{ resource.id }}", template)
        self.assertIn("organization-dialog organization-resource-manager", template)

        self.assertIn(".organization-resource-menu__popover { position: fixed;", styles)
        self.assertIn("z-index: 1100", styles)
        self.assertIn(".organization-dialog { position: fixed; inset: 0;", styles)
        self.assertIn("margin: auto", styles)
        self.assertIn("max-height: calc(100dvh - 48px)", styles)
        self.assertIn(".organization-resource-manager[open]", styles)
        self.assertIn("overflow-y: auto", styles)

    def test_profile_context_is_tenant_scoped_and_members_can_view(self):
        other_org = Organization(name="Other Org", slug="other-org")
        self.db.add(other_org)
        self.db.flush()
        member = User(email="member@example.com", name="Member", organization_id=self.org.id)
        self.db.add(member)
        self.db.flush()
        self.db.add_all([
            OrganizationMembership(organization_id=self.org.id, user_id=member.id, role="member"),
            OrganizationResource(organization_id=self.org.id, title="Visible", resource_type="note", note_content="Visible note"),
            OrganizationResource(organization_id=other_org.id, title="Hidden", resource_type="note", note_content="Hidden note"),
        ])
        self.db.commit()
        member.current_organization_id = self.org.id
        member.current_role = "member"

        context = self._render_context(member, org_id=self.org.id)
        self.assertEqual([resource.title for resource in context["resources"]], ["Visible"])
        self.assertFalse(context["can_manage_organization"])
        self.assertFalse(context["can_manage_resources"])

    def test_profile_lists_actual_connected_systems_and_owner(self):
        self.org.is_live = True
        workspace = Workspace(organization_id=self.org.id, name="Profile Workspace", slug="profile-workspace")
        self.db.add_all([
            workspace,
            SamSourceConfig(organization_id=self.org.id, name="Default SAM.gov Search"),
            GrantsSourceConfig(organization_id=self.org.id, enabled=True),
            OrgProfile(org_id=self.org.id, govwin_credentials_encrypted="stored"),
            SalesforceConnection(workspace_id=self.org.id, status="connected"),
        ])
        self.db.flush()
        self.db.add(ExternalIntegrationConnection(
            workspace_id=workspace.id,
            user_id=self.user.id,
            provider="microsoft",
            connection_status="connected",
        ))
        self.db.commit()

        context = self._render_context(org_id=self.org.id)
        self.assertEqual(context["workspace_status"], "Live")
        self.assertEqual(context["connected_sources"], ["SAM.gov", "Grants.gov", "GovWin"])
        self.assertEqual(context["connected_integrations"], ["Microsoft Graph", "Salesforce"])
        self.assertEqual(context["workspace_owner"]["email"], self.user.email)

    def test_admin_can_create_update_and_delete_resource(self):
        request = self._request(org_id=self.org.id)
        with patch("bidlens.routes.company_profile.get_current_user", return_value=self.user):
            response = asyncio.run(organization_resource_create(
                request,
                title="Proposal Library",
                description="Reusable materials",
                resource_type="link",
                link_url="https://example.com/library",
                note_content="",
                db=self.db,
            ))
        resource = self.db.query(OrganizationResource).one()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(resource.organization_id, self.org.id)

        with patch("bidlens.routes.company_profile.get_current_user", return_value=self.user):
            asyncio.run(organization_resource_update(
                resource.id,
                request,
                title="Proposal Playbook",
                description="Internal guidance",
                resource_type="note",
                link_url="",
                note_content="Use the current review workflow.",
                db=self.db,
            ))
        self.db.refresh(resource)
        self.assertEqual(resource.resource_type, "note")
        self.assertIsNone(resource.link_url)

        with patch("bidlens.routes.company_profile.get_current_user", return_value=self.user):
            asyncio.run(organization_resource_delete(resource.id, request, db=self.db))
        self.assertEqual(self.db.query(OrganizationResource).count(), 0)

    def test_member_cannot_edit_profile_or_resources(self):
        member = User(email="member@profile.test", organization_id=self.org.id)
        self.db.add(member)
        self.db.flush()
        self.db.add(OrganizationMembership(organization_id=self.org.id, user_id=member.id, role="member"))
        self.db.commit()
        member.current_organization_id = self.org.id
        request = self._request(org_id=self.org.id)
        with patch("bidlens.routes.company_profile.get_current_user", return_value=member):
            with self.assertRaises(HTTPException) as save_error:
                asyncio.run(company_profile_save(request, website_url="", db=self.db))
            with self.assertRaises(HTTPException) as resource_error:
                asyncio.run(organization_resource_create(
                    request,
                    title="Forbidden",
                    resource_type="note",
                    note_content="No access",
                    db=self.db,
                ))
        self.assertEqual(save_error.exception.status_code, 403)
        self.assertEqual(resource_error.exception.status_code, 403)

    def test_resource_validation_rejects_unsafe_link(self):
        with patch("bidlens.routes.company_profile.get_current_user", return_value=self.user):
            with self.assertRaises(HTTPException) as error:
                asyncio.run(organization_resource_create(
                    self._request(),
                    title="Unsafe",
                    resource_type="link",
                    link_url="javascript:alert(1)",
                    db=self.db,
                ))
        self.assertEqual(error.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
