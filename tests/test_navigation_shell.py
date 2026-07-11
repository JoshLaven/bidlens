import unittest
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape

from bidlens.routes.settings import _workspace_management_sections


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
            current_organization_name="Test Workspace",
            current_organization_is_live=True,
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
        html = self.env.get_template("administration.html").render(
            request=_Request("/administration"),
            user=self._user(role="admin", platform=True),
            active_page="administration",
            sections=_workspace_management_sections(_Request("/administration"), 7),
        )

        self.assertIn(">Home<", html)
        self.assertIn("Opportunities", html)
        self.assertIn("Triage <em>(Admin only)</em>", html)
        self.assertIn('primary-sidebar-link--management active', html)
        self.assertIn(">Workspace Management<", html)
        self.assertIn("/administration?org_id=7", html)
        self.assertIn("/platform", html)
        self.assertIn(">My Settings<", html)
        self.assertIn(">Logout<", html)

    def test_workspace_management_sections_preserve_org_context(self):
        sections = _workspace_management_sections(_Request("/administration"), 7)
        urls = {section["key"]: section["url"] for section in sections}

        self.assertEqual(urls["organization"], "/company-profile?org_id=7")
        self.assertEqual(urls["members"], "/admin/organizations/7/users?org_id=7")
        self.assertEqual(urls["setup-history"], "/home?org_id=7#setup-history")


if __name__ == "__main__":
    unittest.main()
