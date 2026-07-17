import unittest
from pathlib import Path


TEMPLATES = Path("src/bidlens/templates")


class PageHeaderSystemTests(unittest.TestCase):
    def test_home_uses_editorial_header_only(self):
        source = (TEMPLATES / "home.html").read_text()

        self.assertIn('class="home-brief-header" data-page-header-variant="editorial"', source)
        self.assertIn("{{ daily_brief_first_name }}'s Daily Brief", source)
        self.assertNotIn("workspace_management_hero(", source)
        self.assertNotIn("bidlens-page-header--operational", source)

    def test_queue_macro_defines_light_operational_header(self):
        source = (TEMPLATES / "_queue_layout.html").read_text()

        self.assertIn("bidlens-page-header--operational", source)
        self.assertIn('data-page-header-variant="operational"', source)
        self.assertNotIn("queue-export-button", source)
        self.assertIn("queue_export_action", source)
        self.assertIn('aria-label="Export CSV"', source)
        self.assertIn('title="Export CSV"', source)

    def test_operational_pages_use_shared_header_and_expected_copy(self):
        expected = {
            "feed.html": (
                "Feed",
                "Review active opportunities and move the right ones forward.",
            ),
            "my_shortlist.html": (
                "My Shortlist",
                "Opportunities you are actively considering or pursuing.",
            ),
            "triage.html": (
                "Triage",
                "Review newly imported opportunities before they enter the Feed.",
            ),
            "archive.html": (
                "Archive",
                "Review opportunities you have removed from active consideration.",
            ),
        }

        for filename, (title, description) in expected.items():
            with self.subTest(filename=filename):
                source = (TEMPLATES / filename).read_text()
                self.assertIn(f"queue_heading('{title}', '{description}'", source)
                self.assertNotIn("workspace_management_hero(", source)
                self.assertNotIn('data-page-header-variant="editorial"', source)
                self.assertNotIn("queue-export-button", source)

    def test_existing_csv_export_moves_to_list_actions(self):
        feed = (TEMPLATES / "feed.html").read_text()
        shortlist = (TEMPLATES / "my_shortlist.html").read_text()
        triage = (TEMPLATES / "triage.html").read_text()
        archive = (TEMPLATES / "archive.html").read_text()

        self.assertIn("queue_export_action(export_url)", feed)
        self.assertIn("queue_export_action(export_url)", shortlist)
        self.assertIn("/opportunities/export.csv?view=feed", feed)
        self.assertIn("/opportunities/export.csv?view=my_shortlist", shortlist)
        self.assertIn("data-feed-bulk-actions", feed)
        self.assertIn("data-shortlist-bulk-actions", shortlist)
        self.assertNotIn("queue_export_action", triage)
        self.assertNotIn("queue_export_action", archive)
        self.assertNotIn("/opportunities/export.csv", triage)
        self.assertNotIn("/opportunities/export.csv", archive)

    def test_workspace_management_pages_use_administrative_hero(self):
        expected = {
            "company_profile.html": "Company information used for opportunity matching, enrichment, and routing.",
            "workspace_members.html": "Invite users and manage access to this workspace.",
            "govwin_import.html": "Configure where BidLens discovers and imports opportunities.",
            "pursuit_lanes.html": "Control how opportunities enter the Feed and how they are organized.",
            "import_history.html": "Review opportunity imports and source-processing activity.",
            "integrations.html": "Connect and manage the systems BidLens works with.",
            "market_activity.html": "Explore market activity and the organizational intelligence BidLens has captured.",
        }

        for filename, description in expected.items():
            with self.subTest(filename=filename):
                source = (TEMPLATES / filename).read_text()
                self.assertIn("_workspace_management_hero.html", source)
                self.assertIn("workspace_management_hero(", source)
                self.assertIn(description, source)
                self.assertNotIn("bidlens-page-header--operational", source)

    def test_header_variant_css_exists(self):
        source = Path("src/bidlens/static/css/styles.css").read_text()

        self.assertIn(".bidlens-page-header--operational", source)
        self.assertIn(".workspace-management-hero", source)
        self.assertIn(".home-brief-header", source)


if __name__ == "__main__":
    unittest.main()
