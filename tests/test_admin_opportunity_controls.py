import unittest
from pathlib import Path


TEMPLATES = Path("src/bidlens/templates")
STYLES = Path("src/bidlens/static/css/styles.css")
ROUTES = Path("src/bidlens/routes/opportunities.py")


class AdminOpportunityControlsTests(unittest.TestCase):
    def setUp(self):
        self.feed = (TEMPLATES / "feed.html").read_text()
        self.shortlist = (TEMPLATES / "my_shortlist.html").read_text()
        self.triage = (TEMPLATES / "triage.html").read_text()
        self.archive = (TEMPLATES / "archive.html").read_text()
        self.toolbar = (TEMPLATES / "_queue_layout.html").read_text()
        self.styles = STYLES.read_text()
        self.routes = ROUTES.read_text()

    def test_shared_admin_filter_bar_renders_stage_source_lane_in_order(self):
        self.assertIn("queue-filter-stack--admin", self.toolbar)
        admin_branch = self.toolbar[self.toolbar.index("queue-filter-stack--admin") :]
        self.assertLess(admin_branch.index("stage_filter_chips("), admin_branch.index("source_filter_chips("))
        self.assertLess(admin_branch.index("source_filter_chips("), admin_branch.index("lane_filter_chips("))
        self.assertIn("Stage", self.toolbar)
        self.assertIn("Source", self.toolbar)
        self.assertIn("Lane", self.toolbar)
        self.assertIn("No active lanes", self.toolbar)

    def test_admin_views_use_shared_stacked_filters(self):
        for source in (self.feed, self.shortlist):
            with self.subTest(template=source[:30]):
                self.assertIn("admin_filter_bar=(user.current_role == 'admin')", source)
                self.assertIn("source_options=source_options if user.current_role == 'admin' else none", source)
                self.assertIn("selected_sources=selected_sources if user.current_role == 'admin' else none", source)

        self.assertIn("admin_filter_bar=(user.current_role == 'admin')", self.archive)
        self.assertIn("source_options if user.current_role == 'admin' else none", self.archive)
        self.assertIn("selected_sources if user.current_role == 'admin' else none", self.archive)
        self.assertIn("admin_filter_bar=true", self.triage)
        self.assertIn("active_lanes", self.triage)

    def test_regular_user_archive_does_not_get_admin_source_or_lane_filters(self):
        self.assertIn("lane_id if user.current_role == 'admin' else none", self.archive)
        self.assertIn("active_lanes if user.current_role == 'admin' else none", self.archive)
        self.assertIn("sources_value if user.current_role == 'admin' else none", self.archive)

    def test_triage_bulk_controls_are_select_all_selected_actions_and_export(self):
        self.assertIn("data-triage-select-all", self.triage)
        self.assertIn("feed-results-left", self.triage)
        self.assertIn("Select All", self.triage)
        self.assertIn("queue_export_action(export_url)", self.triage)
        self.assertIn("/opportunities/export.csv?view=triage", self.triage)
        self.assertIn("data-triage-selected=\"qualify\"", self.triage)
        self.assertIn("data-triage-selected=\"reject\"", self.triage)
        self.assertNotIn("data-triage-visible", self.triage)
        self.assertNotIn("Qualify all visible", self.triage)
        self.assertNotIn("Reject all visible", self.triage)
        self.assertNotIn("mode === 'visible'", self.triage)

    def test_triage_source_shortcuts_are_removed(self):
        self.assertNotIn("_source_controls.html", self.triage)
        self.assertIn("_source_controls.html", self.feed)

    def test_triage_selected_card_reuses_green_card_accent(self):
        self.assertIn('[data-card-view="triage"])::before', self.styles)
        self.assertNotIn(":has(.opp-card-select input:checked)", self.styles)
        self.assertNotIn("opp-card--selected", self.styles)

    def test_admin_filter_parameters_are_supported_by_routes_and_export(self):
        self.assertIn("async def feed(", self.routes)
        self.assertIn("sources: str | None = None", self.routes)
        self.assertIn("async def triage_queue(", self.routes)
        self.assertIn("lane_id: str | None = None", self.routes)
        self.assertIn('if view == "triage" and not _is_admin(user):', self.routes)
        self.assertIn('elif view == "triage":', self.routes)
        self.assertIn("sources=sources", self.routes)


if __name__ == "__main__":
    unittest.main()
