import unittest
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader


TEMPLATES = Path("src/bidlens/templates")


class WorkspaceManagementShellTests(unittest.TestCase):
    def setUp(self):
        self.environment = Environment(loader=FileSystemLoader(TEMPLATES))

    def _render_shell(self, *, role="admin", live=True, platform=False, path="/company-profile"):
        user = SimpleNamespace(
            name="Test User",
            email="test@example.com",
            current_role=role,
            current_organization_id=7,
            current_organization_name="Test Workspace",
            current_organization_is_live=live,
            organization_id=7,
            triage_unreviewed_count=0,
            is_platform_admin=platform,
            organization=None,
        )
        request = SimpleNamespace(
            query_params={"org_id": "7"},
            url=SimpleNamespace(path=path, query="org_id=7"),
        )
        template = self.environment.from_string(
            "{% extends 'base.html' %}{% block content %}{% endblock %}"
        )
        return template.render(
            request=request,
            user=user,
            active_page="platform" if path == "/platform" else "company_profile",
        )

    def test_shared_hero_is_used_by_every_workspace_management_destination(self):
        expected = {
            "company_profile.html": "Organization",
            "workspace_members.html": "Users",
            "govwin_import.html": "Opportunity Sources",
            "pursuit_lanes.html": "Feed Settings",
            "import_history.html": "Import History",
            "integrations.html": "Integrations",
            "market_activity.html": "Insights",
        }

        for filename, title in expected.items():
            with self.subTest(filename=filename):
                source = (TEMPLATES / filename).read_text()
                self.assertIn("_workspace_management_hero.html", source)
                self.assertIn(f"workspace_management_hero('{title}'", source)
                self.assertIn("workspace-management-page", source)

    def test_operational_pages_do_not_use_workspace_management_hero(self):
        for filename in ("home.html", "feed.html", "my_shortlist.html", "triage.html", "archive.html"):
            with self.subTest(filename=filename):
                source = (TEMPLATES / filename).read_text()
                self.assertNotIn("_workspace_management_hero.html", source)
                self.assertNotIn("workspace_management_hero(", source)

    def test_shared_hero_badge_is_optional(self):
        source = (TEMPLATES / "_workspace_management_hero.html").read_text()
        self.assertIn("{% if updated_at %}", source)
        self.assertIn("data-workspace-management-hero", source)

    def test_sidebar_keeps_accordion_groups_routes_and_collapse_control(self):
        source = (TEMPLATES / "base.html").read_text()
        self.assertIn('<details class="primary-sidebar-management"', source)
        self.assertNotIn("primary-sidebar-subnav-label", source)
        self.assertIn("primary-sidebar-subnav-divider", source)
        self.assertNotIn(">Organization</span>", source)
        self.assertNotIn(">Opportunities</span>", source)
        for label in (
            "Overview",
            "Users",
            "Opportunity Sources",
            "Feed Settings",
            "Import History",
            "Integrations",
            "Insights",
        ):
            self.assertIn(f">{label}</strong>", source)
        for route in (
            "/company-profile",
            "/admin/organizations/",
            "/opportunity-discovery",
            "/settings",
            "/imports/history",
            "/integrations",
            "/admin/market-activity",
        ):
            self.assertIn(route, source)
        self.assertIn("data-primary-sidebar-toggle", source)
        self.assertIn("sidebar-collapse-icon", source)
        self.assertIn("data-work-sidebar-toggle", source)
        self.assertIn("Collapse navigation", source)
        self.assertIn("Expand navigation", source)
        self.assertIn("bidlens.primaryNavigationCollapsed", source)
        self.assertIn('data-rail-label="OS"', source)
        self.assertIn("<strong>Workspace</strong>", source)
        self.assertNotIn("<strong>Workspace Management</strong>", source)

    def test_workspace_navigation_remains_admin_only(self):
        admin_html = self._render_shell(role="admin")
        member_html = self._render_shell(role="member")

        self.assertIn(">Workspace</strong>", admin_html)
        self.assertIn("/company-profile?org_id=7", admin_html)
        self.assertNotIn(">Workspace</strong>", member_html)
        self.assertNotIn("/company-profile?org_id=7", member_html)

    def test_pre_live_and_platform_shells_preserve_existing_navigation_modes(self):
        pre_live_html = self._render_shell(role="admin", live=False, path="/organization-setup")
        platform_html = self._render_shell(role="admin", platform=True, path="/platform")

        self.assertIn("primary-sidebar--onboarding", pre_live_html)
        self.assertNotIn(">Workspace</strong>", pre_live_html)
        self.assertIn('aria-label="Platform navigation"', platform_html)
        self.assertNotIn(">Workspace</strong>", platform_html)

    def test_sidebar_nav_scrolls_independently_from_pinned_profile(self):
        css = Path("src/bidlens/static/css/styles.css").read_text()

        self.assertIn(".primary-sidebar-nav", css)
        self.assertIn("height: 100vh;", css)
        self.assertIn("overflow: hidden;", css)
        self.assertIn("align-content: start;", css)
        self.assertIn("grid-auto-rows: max-content;", css)
        self.assertIn("overflow-y: auto;", css)
        self.assertIn("overscroll-behavior: contain;", css)
        self.assertIn("min-height: 0;", css)
        self.assertIn(".primary-sidebar-brand {\n    display: flex;\n    flex: 0 0 auto;", css)
        self.assertIn(".primary-sidebar-user {\n    flex: 0 0 auto;", css)

        source = (TEMPLATES / "base.html").read_text()
        self.assertIn("keepActiveNavigationVisible", source)
        self.assertIn("scrollIntoView({ block: 'nearest', inline: 'nearest' })", source)


if __name__ == "__main__":
    unittest.main()
