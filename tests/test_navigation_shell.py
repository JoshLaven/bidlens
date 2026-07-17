import asyncio
from datetime import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from jinja2 import Environment, FileSystemLoader, select_autoescape

from bidlens.routes import settings


class _Url:
    def __init__(self, path="/", query="org_id=7"):
        self.path = path
        self.query = query


class _Request:
    def __init__(self, path="/", query_params=None, query="org_id=7"):
        self.url = _Url(path, query)
        self.query_params = query_params if query_params is not None else {"org_id": "7"}


class NavigationShellTests(unittest.TestCase):
    def setUp(self):
        self.env = Environment(
            loader=FileSystemLoader("src/bidlens/templates"),
            autoescape=select_autoescape(["html"]),
        )

    def _user(self, *, role="admin", platform=False):
        return SimpleNamespace(
            name="Test User",
            email="test@example.com",
            current_role=role,
            current_organization_id=7,
            current_organization_name="Test Workspace",
            current_organization_is_live=True,
            organization_id=7,
            triage_unreviewed_count=4,
            is_platform_admin=platform,
            organization=None,
        )

    def test_primary_sidebar_hides_triage_and_workspace_management_for_members(self):
        html = self.env.get_template("feed.html").render(
            request=_Request("/"),
            user=self._user(role="member"),
            active_page="feed",
            sidebar={},
            opportunities=[],
            sort="imported",
            direction="desc",
            q="",
            lane_id=None,
            active_lanes=[],
            my_lanes=[],
            stages_value="",
            selected_stages=(),
            result_count=0,
            page=1,
            total_pages=1,
            page_size=50,
            triage_enabled=False,
            now=None,
            org_id_param="7",
        )

        self.assertIn('class="primary-sidebar"', html)
        self.assertIn(">Home<", html)
        self.assertIn("Opportunities", html)
        self.assertIn(">Feed<", html)
        self.assertIn(">My Shortlist<", html)
        self.assertIn(">Archive<", html)
        self.assertNotIn("Triage <em>(Admin only)</em>", html)
        self.assertNotIn("Workspace Management", html)

    def test_admin_sidebar_includes_admin_destinations(self):
        html = self.env.from_string("{% extends 'base.html' %}{% block content %}{% endblock %}").render(
            request=_Request("/opportunity-discovery"),
            user=self._user(role="admin", platform=True),
            active_page="imports",
        )

        self.assertIn(">Home<", html)
        self.assertIn("Opportunities", html)
        self.assertIn("Triage <em>(Admin only)</em>", html)
        self.assertIn('primary-sidebar-link--management active', html)
        self.assertIn(">Workspace Management<", html)
        self.assertIn("<details class=\"primary-sidebar-management\" open>", html)
        self.assertIn("/company-profile?org_id=7", html)
        self.assertIn("/admin/organizations/7/users?org_id=7", html)
        self.assertIn("/opportunity-discovery?org_id=7", html)
        self.assertIn("/integrations?org_id=7", html)
        self.assertIn("/settings?org_id=7", html)
        self.assertIn("/admin/market-activity?org_id=7", html)
        self.assertNotIn('>Organization</span>', html)
        self.assertNotIn('>Opportunities</span>', html)
        self.assertIn('primary-sidebar-subnav-divider', html)
        self.assertIn('>Overview</strong>', html)
        self.assertIn('>Users</strong>', html)
        self.assertIn('>Opportunity Sources</strong>', html)
        self.assertIn('>Feed Settings</strong>', html)
        self.assertIn('>Import History</strong>', html)
        self.assertIn('>Integrations</strong>', html)
        self.assertIn('>Insights</strong>', html)
        self.assertIn('class="active" aria-current="page" title="Opportunity Sources"', html)
        self.assertNotIn('Pursuit Lanes</a>', html)
        self.assertIn('Import History</strong>', html)
        self.assertIn('data-primary-sidebar-toggle', html)
        self.assertIn('aria-label="Collapse navigation"', html)
        self.assertIn("/platform", html)
        self.assertIn(">My Settings<", html)
        self.assertIn(">Logout<", html)

    def test_admin_sidebar_marks_analytics_active(self):
        html = self.env.from_string("{% extends 'base.html' %}{% block content %}{% endblock %}").render(
            request=_Request("/admin/market-activity"),
            user=self._user(role="admin"),
            active_page="imports",
        )

        self.assertIn("<details class=\"primary-sidebar-management\" open>", html)
        self.assertIn(
            'class="active" aria-current="page" title="Insights"',
            html,
        )
        self.assertNotIn('class="active" aria-current="page" title="Import History"', html)

    def test_pull_history_marks_history_active(self):
        html = self.env.from_string("{% extends 'base.html' %}{% block content %}{% endblock %}").render(
            request=_Request("/imports/history"),
            user=self._user(role="admin"),
            active_page="imports",
        )

        self.assertIn(
            'class="active" aria-current="page" title="Import History"',
            html,
        )

    def test_pursuit_lanes_marks_feed_settings_active(self):
        html = self.env.from_string("{% extends 'base.html' %}{% block content %}{% endblock %}").render(
            request=_Request("/pursuit-lanes"),
            user=self._user(role="admin"),
            active_page="pursuit_lanes",
        )

        self.assertIn(
            'class="active" aria-current="page" title="Feed Settings"',
            html,
        )

    def test_pre_live_admin_gets_onboarding_shell_without_app_navigation(self):
        user = self._user(role="admin")
        user.current_organization_is_live = False

        html = self.env.get_template("organization_setup.html").render(
            request=_Request("/organization-setup"),
            user=user,
            active_page="home",
            home={
                "workspace_summary": {
                    "organization_id": 7,
                    "organization_name": "Test Workspace",
                    "headline": "Welcome to BidLens.",
                    "description": "Let’s get your organization ready.",
                },
                "operational_snapshot": {},
                "recommendations": [],
                "completed": [],
                "can_go_live": False,
            },
        )

        self.assertIn('class="primary-sidebar primary-sidebar--onboarding"', html)
        self.assertIn("Organization Setup", html)
        self.assertIn('href="/organization-setup?org_id=7"', html)
        self.assertIn("Test Workspace", html)
        self.assertNotIn("Open Feed", html)
        self.assertNotIn('href="/?org_id=7"', html)
        self.assertNotIn(">Home<", html)
        self.assertNotIn(">Feed<", html)
        self.assertNotIn(">My Shortlist<", html)
        self.assertNotIn("Triage <em>(Admin only)</em>", html)
        self.assertNotIn(">Archive<", html)
        self.assertNotIn(">Workspace Management<", html)
        self.assertNotIn(">My Settings<", html)
        self.assertIn(">Logout<", html)

    def test_live_admin_keeps_full_application_sidebar(self):
        html = self.env.from_string("{% extends 'base.html' %}{% block content %}{% endblock %}").render(
            request=_Request("/home"),
            user=self._user(role="admin"),
            active_page="home",
        )

        self.assertNotIn("primary-sidebar--onboarding", html)
        self.assertIn(">Home<", html)
        self.assertIn(">Feed<", html)
        self.assertIn(">My Shortlist<", html)
        self.assertIn("Triage <em>(Admin only)</em>", html)
        self.assertIn(">Archive<", html)
        self.assertIn(">Workspace Management<", html)

    def test_setup_back_link_only_renders_for_pre_live_workspace(self):
        template = self.env.from_string(
            "{% from '_setup_back_link.html' import setup_back_link with context %}{{ setup_back_link() }}"
        )
        pre_live_user = self._user(role="admin")
        pre_live_user.current_organization_is_live = False
        live_user = self._user(role="admin")

        pre_live_html = template.render(request=_Request("/settings"), user=pre_live_user)
        live_html = template.render(request=_Request("/settings"), user=live_user)

        self.assertIn("← Back to Setup", pre_live_html)
        self.assertIn('class="setup-back-link"', pre_live_html)
        self.assertIn('href="/organization-setup?org_id=7"', pre_live_html)
        self.assertNotIn("Back to Setup", live_html)

    def test_completed_setup_items_remain_editable(self):
        user = self._user(role="admin")
        user.current_organization_is_live = False

        html = self.env.get_template("organization_setup.html").render(
            request=_Request("/organization-setup"),
            user=user,
            active_page="home",
            home={
                "workspace_summary": {
                    "organization_id": 7,
                    "organization_name": "Test Workspace",
                    "headline": "Welcome to BidLens.",
                    "description": "Let’s get your organization ready.",
                },
                "operational_snapshot": {},
                "recommendations": [],
                "completed": [
                    {
                        "title": "Users invited",
                        "description": "A user invitation is pending.",
                        "completed_at": None,
                        "cta_label": "Edit",
                        "cta_url": "/admin/organizations/7/users?org_id=7",
                    }
                ],
                "can_go_live": False,
            },
        )

        self.assertIn("✓", html)
        self.assertIn("Users invited", html)
        self.assertIn('href="/admin/organizations/7/users?org_id=7"', html)
        self.assertIn("home-next-step--link", html)
        self.assertIn("home-next-step-chevron", html)
        self.assertNotIn("Edit →", html)

    def test_completed_created_item_renders_as_audit_trail_not_navigation(self):
        user = self._user(role="admin")
        user.current_organization_is_live = False

        html = self.env.get_template("organization_setup.html").render(
            request=_Request("/organization-setup"),
            user=user,
            active_page="home",
            home={
                "workspace_summary": {
                    "organization_id": 7,
                    "organization_name": "Test Workspace",
                    "headline": "Welcome to BidLens.",
                    "description": "Let’s get your organization ready.",
                },
                "operational_snapshot": {},
                "recommendations": [],
                "completed": [
                    {
                        "key": "organization-created",
                        "title": "Organization Created",
                        "description": None,
                        "completed_at": datetime(2026, 7, 17),
                        "cta_url": None,
                    }
                ],
                "can_go_live": False,
            },
        )

        self.assertIn("Organization Created", html)
        self.assertIn("Completed Jul 17, 2026", html)
        self.assertIn('<article class="home-next-step home-completed-item">', html)
        self.assertNotIn('href="/company-profile', html)
        self.assertNotIn("home-next-step-chevron", html)

    def test_opportunity_sources_hides_operational_sections_during_setup_only(self):
        base_context = {
            "active_page": "imports",
            "sidebar": {},
            "latest_runs": {},
            "recent_activity": [],
            "sam_config": None,
            "sam_configs": [],
            "grants_config": None,
            "result": None,
            "error": None,
        }
        setup_user = self._user(role="admin")
        setup_user.current_organization_is_live = False
        live_user = self._user(role="admin")

        setup_html = self.env.get_template("govwin_import.html").render(
            request=_Request("/opportunity-discovery"),
            user=setup_user,
            **base_context,
        )
        management_html = self.env.get_template("govwin_import.html").render(
            request=_Request("/opportunity-discovery"),
            user=live_user,
            **base_context,
        )

        self.assertIn("SAM.gov", setup_html)
        self.assertIn("Grants.gov", setup_html)
        self.assertIn("GovWin", setup_html)
        self.assertNotIn('id="manual-import"', setup_html)
        self.assertNotIn("Manual Opportunity Import", setup_html)
        self.assertNotIn('id="operational-history"', setup_html)
        self.assertNotIn("Recent Activity", setup_html)

        self.assertIn('id="manual-import"', management_html)
        self.assertIn("Manual Opportunity Import", management_html)
        self.assertIn('id="operational-history"', management_html)
        self.assertIn("Recent Activity", management_html)

    def test_setup_management_pages_remove_redundant_single_section_eyebrows(self):
        users_template = self.env.loader.get_source(self.env, "workspace_members.html")[0]
        feed_template = self.env.loader.get_source(self.env, "pursuit_lanes.html")[0]
        outbound_template = self.env.loader.get_source(self.env, "outbound_integrations.html")[0]

        self.assertIn("Add Users", users_template)
        self.assertNotIn("<span>Access</span>", users_template)
        self.assertIn("Pursuit Lanes", feed_template)
        self.assertNotIn("<span>Workspace organization</span>", feed_template)
        self.assertIn("Where should BidLens send information?", outbound_template)
        self.assertNotIn("<span>Business systems</span>", outbound_template)

    def test_setup_checklist_rows_are_clickable_without_action_buttons(self):
        user = self._user(role="admin")
        user.current_organization_is_live = False

        html = self.env.get_template("organization_setup.html").render(
            request=_Request("/organization-setup"),
            user=user,
            active_page="home",
            home={
                "workspace_summary": {
                    "organization_id": 7,
                    "organization_name": "Test Workspace",
                    "headline": "Welcome to BidLens.",
                    "description": "Let’s get your organization ready.",
                },
                "operational_snapshot": {},
                "recommendations": [
                    {
                        "title": "Enable Opportunity Discovery",
                        "description": "Tell BidLens where to discover opportunities.",
                        "label": "Required",
                        "cta_url": "/opportunity-discovery?org_id=7",
                    }
                ],
                "completed": [],
                "can_go_live": False,
            },
        )

        self.assertIn('<span id="recommendations-heading">Next Steps</span>', html)
        self.assertNotIn("Recommended Next Steps", html)
        self.assertNotIn("Only the actions that still move this workspace forward.", html)
        self.assertIn('href="/opportunity-discovery?org_id=7"', html)
        self.assertIn("home-next-step--link", html)
        self.assertIn("home-next-step-chevron", html)
        self.assertNotIn("btn btn-sm btn-outline-secondary\">Opportunity Discovery", html)

    def test_administration_redirects_to_organization(self):
        user = self._user(role="admin")
        with patch.object(settings, "require_user", return_value=user):
            response = asyncio.run(settings.administration_page(
                _Request("/administration", query="org_id=7"),
                db=MagicMock(),
            ))

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/company-profile?org_id=7")


if __name__ == "__main__":
    unittest.main()
