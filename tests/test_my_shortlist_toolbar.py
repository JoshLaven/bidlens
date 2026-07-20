import unittest
from pathlib import Path


class MyShortlistToolbarTemplateTests(unittest.TestCase):
    def setUp(self):
        self.template = Path("src/bidlens/templates/my_shortlist.html").read_text()
        self.card_template = Path("src/bidlens/templates/_opp_card.html").read_text()
        self.styles = Path("src/bidlens/static/css/styles.css").read_text()

    def test_shortlist_uses_feed_role_aware_toolbar(self):
        self.assertIn("queue_toolbar('/my-shortlist'", self.template)
        self.assertIn("show_filters=(user.current_role == 'admin')", self.template)
        self.assertIn("feed_sort_options=true", self.template)

    def test_shortlist_results_row_matches_feed_bulk_selection_pattern(self):
        self.assertIn("feed-results-left", self.template)
        self.assertIn("data-shortlist-select-all", self.template)
        self.assertIn("data-shortlist-archive-selected", self.template)
        self.assertIn("Archive selected", self.template)
        self.assertNotIn("data-archive-visible", self.template)
        self.assertNotIn("Archive all visible", self.template)

    def test_shortlist_export_is_list_level_icon_action(self):
        self.assertIn("queue_export_action(export_url)", self.template)
        self.assertIn("/opportunities/export.csv?view=my_shortlist", self.template)
        self.assertIn("data-shortlist-bulk-actions", self.template)
        self.assertNotIn("queue_heading('My Shortlist', 'Opportunities you are actively considering or pursuing.', export_url)", self.template)

    def test_shortlist_cards_expose_bulk_archive_checkboxes(self):
        self.assertIn("view in ['feed', 'triage', 'my_shortlist']", self.card_template)
        self.assertIn("data-shortlist-archive-checkbox", self.card_template)
        self.assertIn("'triage' if view == 'triage' else 'archive'", self.card_template)

    def test_shortlist_cards_reuse_feed_checkbox_spacing(self):
        self.assertIn(
            '.opp-card:is([data-card-view="feed"], [data-card-view="my_shortlist"], [data-card-view="triage"], [data-card-view="user_archive"])',
            self.styles,
        )
        self.assertIn("padding-left: 44px;", self.styles)


if __name__ == "__main__":
    unittest.main()
