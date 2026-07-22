import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from bidlens.routes import connect_sources, imports


class _Request:
    def __init__(self, path="/opportunity-discovery", query_params=None):
        self.query_params = query_params if query_params is not None else {"org_id": "7"}
        self.url = SimpleNamespace(path=path, query="org_id=7")


class OpportunityDiscoveryNavigationTests(unittest.TestCase):
    def _admin(self):
        return SimpleNamespace(
            id=1,
            organization_id=7,
            current_organization_id=7,
            current_role="admin",
        )

    def test_legacy_govwin_import_get_redirects_to_manual_import_section(self):
        with patch.object(imports, "require_admin", return_value=self._admin()):
            response = asyncio.run(imports.govwin_import_page(_Request("/imports/govwin"), db=MagicMock()))

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/opportunity-discovery?org_id=7#manual-import",
        )

    def test_legacy_connect_sources_get_redirects_to_opportunity_discovery(self):
        with patch.object(connect_sources, "require_admin", return_value=self._admin()):
            response = asyncio.run(connect_sources.connect_sources_page(
                _Request("/connect-sources"),
                db=MagicMock(),
            ))

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/opportunity-discovery?org_id=7")

    def test_member_is_blocked_from_legacy_source_management_get(self):
        with patch.object(
            imports,
            "require_admin",
            side_effect=HTTPException(status_code=403, detail="admin only"),
        ):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(imports.govwin_import_page(_Request("/imports/govwin"), db=MagicMock()))

        self.assertEqual(raised.exception.status_code, 403)

    def test_source_management_links_point_to_canonical_destinations(self):
        template = Path("src/bidlens/templates/govwin_import.html").read_text()
        triage_controls = Path("src/bidlens/templates/_source_controls.html").read_text()

        self.assertIn("{% block title %}Opportunity Sources - BidLens{% endblock %}", template)
        self.assertIn('id="opportunity-sources"', template)
        self.assertIn('id="manual-import"', template)
        self.assertIn("/admin/sources/sam", template)
        self.assertIn('id="grants-gov"', template)
        self.assertIn("/connect-sources/grants/enable", template)
        self.assertIn("Opportunity Sources", template)
        self.assertNotIn('aria-label="Opportunity Sources views"', template)
        self.assertNotIn("market-view-tabs", template)
        self.assertIn("/opportunity-discovery", triage_controls)
        self.assertIn("#manual-import", triage_controls)

    def test_users_setup_header_keeps_back_link_without_updated_badge(self):
        template = Path("src/bidlens/templates/workspace_members.html").read_text()

        self.assertIn("action_url=setup_back_url", template)
        self.assertIn("workspace_management_hero('Users'", template)
        self.assertNotIn("workspace.updated_at", template)

    def test_govwin_source_is_future_placeholder_and_manual_import_is_generic_csv(self):
        template = Path("src/bidlens/templates/govwin_import.html").read_text()

        self.assertIn("<h3>GovWin</h3>", template)
        self.assertIn("Commercial market intelligence source.", template)
        self.assertIn("Not yet available", template)
        self.assertIn("Coming in a future release", template)
        self.assertIn("Default Window", template)
        self.assertIn("Not applicable", template)
        self.assertIn("Coming Soon", template)
        self.assertNotIn("Import Workbook", template)
        self.assertNotIn("workbook import is available below", template)

        self.assertIn("Opportunity File Upload", template)
        self.assertIn("BidLens CSV template", template)
        self.assertIn("/imports/manual/template.csv", template)
        self.assertIn('accept=".csv,text/csv"', template)
        self.assertIn("Import Opportunities", template)
        self.assertIn("source-template-button", template)
        self.assertIn("Template</a>", template)
        self.assertIn('class="sr-only" for="manual-import-file"', template)
        self.assertNotIn("Upload opportunity file</label>", template)
        self.assertNotIn("Accepted Format", template)
        self.assertNotIn(".csv BidLens template</dd>", template)
        self.assertNotIn("GovWin export schema", template)
        self.assertNotIn("GovWin Staging Name", template)
        self.assertNotIn(".xlsx GovWin export", template)


if __name__ == "__main__":
    unittest.main()
