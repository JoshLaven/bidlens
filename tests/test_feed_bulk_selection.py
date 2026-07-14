import unittest
from pathlib import Path


class FeedBulkSelectionTemplateTests(unittest.TestCase):
    def setUp(self):
        self.template = Path("src/bidlens/templates/feed.html").read_text()

    def test_feed_uses_select_all_instead_of_archive_all_visible(self):
        self.assertIn("data-feed-select-all", self.template)
        self.assertIn("feed-results-left", self.template)
        self.assertIn("Select All", self.template)
        self.assertIn("data-archive-selected", self.template)
        self.assertNotIn("data-archive-visible", self.template)
        self.assertNotIn("Archive all visible", self.template)

    def test_bulk_archive_only_uses_checked_opportunities(self):
        self.assertIn("checkboxes.filter((checkbox) => checkbox.checked)", self.template)
        self.assertNotIn("mode === 'visible'", self.template)
        self.assertNotIn("Archive all ${oppIds.length} visible opportunities?", self.template)


if __name__ == "__main__":
    unittest.main()
