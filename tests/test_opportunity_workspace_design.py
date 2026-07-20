import unittest
from pathlib import Path


TEMPLATES = Path("src/bidlens/templates")
STYLES = Path("src/bidlens/static/css/styles.css")
ROUTES = Path("src/bidlens/routes/opportunities.py")


class OpportunityWorkspaceDesignTests(unittest.TestCase):
    def setUp(self):
        self.card = (TEMPLATES / "_opp_card.html").read_text()
        self.toolbar = (TEMPLATES / "_queue_layout.html").read_text()
        self.archive = (TEMPLATES / "archive.html").read_text()
        self.triage = (TEMPLATES / "triage.html").read_text()
        self.styles = STYLES.read_text()
        self.routes = ROUTES.read_text()

    def test_archive_renders_shared_opportunity_card(self):
        self.assertIn("opp_card(opp, view='user_archive'", self.archive)
        self.assertIn("data-card-view=\"{{ view }}\"", self.card)
        self.assertIn("Restore to Feed", self.card)

    def test_archive_card_uses_canonical_card_proportions(self):
        self.assertIn('[data-card-view="user_archive"]', self.styles)
        self.assertIn(
            '.opp-card:is([data-card-view="feed"], [data-card-view="my_shortlist"], [data-card-view="triage"], [data-card-view="user_archive"]) .opp-card-top',
            self.styles,
        )
        self.assertIn(
            '.opp-card:is([data-card-view="feed"], [data-card-view="my_shortlist"], [data-card-view="triage"], [data-card-view="user_archive"]) {\n  padding-top: 9px;\n  padding-bottom: 7px;\n  padding-left: 44px;',
            self.styles,
        )
        self.assertIn("view in ['feed', 'my_shortlist', 'user_archive']", self.card)

    def test_my_shortlist_preserves_checked_interested_state(self):
        self.assertIn("view='my_shortlist'", (TEMPLATES / "my_shortlist.html").read_text())
        self.assertIn("&#10003; Interested", self.card)
        self.assertIn("Archive", self.card)

    def test_archive_preserves_rich_details_in_expanded_section(self):
        self.assertIn("view in ['my_shortlist', 'user_archive'] and opp.account_type", self.card)
        self.assertIn("view in ['my_shortlist', 'user_archive'] and pursuit_lanes", self.card)
        self.assertIn("Open in Salesforce", self.card)

    def test_shared_toolbar_stacks_stage_then_source_filters(self):
        self.assertIn("source_options=none", self.toolbar)
        self.assertIn("show_tabs=true", self.toolbar)
        self.assertIn("admin_filter_bar=false", self.toolbar)
        self.assertIn("queue-filter-stack", self.toolbar)
        self.assertIn("queue-filter-stack--admin", self.toolbar)
        toolbar_body = self.toolbar[self.toolbar.index("{% macro queue_toolbar("):]
        self.assertLess(
            toolbar_body.index("stage_filter_chips(route"),
            toolbar_body.index("source_filter_chips(route"),
        )
        self.assertLess(
            toolbar_body.index("source_filter_chips(route"),
            toolbar_body.index("lane_filter_chips(route"),
        )
        self.assertIn("justify-content: flex-end;", self.styles)
        self.assertIn(".queue-filter-stack", self.styles)

    def test_triage_uses_shared_toolbar_for_source_filter(self):
        self.assertNotIn("triage-source-filter-row", self.triage)
        self.assertNotIn("triage-source-filter-row", self.styles)
        self.assertIn("source_options, selected_sources, admin_filter_bar=true", self.triage)
        self.assertNotIn('{% include "_source_controls.html" %}', self.triage)

    def test_archive_uses_role_aware_shared_toolbar_filters(self):
        self.assertIn("selected_stages", self.archive)
        self.assertIn("source_options if user.current_role == 'admin' else none", self.archive)
        self.assertIn("sources_value if user.current_role == 'admin' else none", self.archive)
        self.assertIn("active_lanes if user.current_role == 'admin' else none", self.archive)
        self.assertIn("admin_filter_bar=(user.current_role == 'admin')", self.archive)

    def test_archive_route_defaults_filters_to_all_without_changing_dataset(self):
        self.assertIn("selected_stages = _normalize_stage_filters(", self.routes)
        self.assertIn("selected_sources = (", self.routes)
        self.assertIn("if _is_admin(user):\n        query = _apply_triage_source_filter", self.routes)
        self.assertIn('"source_options": TRIAGE_SOURCE_OPTIONS if _is_admin(user) else None', self.routes)


if __name__ == "__main__":
    unittest.main()
