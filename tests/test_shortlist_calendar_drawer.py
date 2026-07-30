import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from bidlens.routes.opportunities import _calendar_drawer_items


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "bidlens" / "templates"
STATIC = ROOT / "src" / "bidlens" / "static"


class ShortlistCalendarDrawerTests(unittest.TestCase):
    def test_calendar_payload_contains_deadlined_opportunities_only(self):
        rows = [
            (SimpleNamespace(id=10, title="NIH Opportunity", response_deadline=date(2026, 7, 18)), False),
            (SimpleNamespace(id=11, title="No Deadline", response_deadline=None), False),
            (SimpleNamespace(id=12, title="CDC Opportunity", response_deadline=date(2026, 7, 18)), False),
        ]

        items = _calendar_drawer_items(rows)

        self.assertEqual([item["id"] for item in items], [10, 12])
        self.assertEqual([item["deadline"] for item in items], ["2026-07-18", "2026-07-18"])
        self.assertEqual(items[0]["url"], "/opportunity/10?return_to=shortlist")

    def test_shortlist_renders_reusable_drawer_with_server_payload(self):
        shortlist = (TEMPLATES / "my_shortlist.html").read_text()
        component = (TEMPLATES / "_calendar_drawer.html").read_text()

        self.assertIn("calendar_drawer(calendar_items", shortlist)
        self.assertIn("data-calendar-drawer-toggle", component)
        self.assertIn("data-calendar-items", component)
        self.assertIn("data-calendar-grid", component)
        self.assertNotIn("Upcoming Deadlines", component)

    def test_drawer_interactions_preserve_page_and_support_keyboard(self):
        script = (STATIC / "js" / "calendar_drawer.js").read_text()

        self.assertIn("window.sessionStorage", script)
        self.assertIn("event.key === 'Escape'", script)
        self.assertIn("ArrowLeft", script)
        self.assertIn("preventScroll: true", script)
        self.assertNotIn("window.location", script)
        self.assertNotIn("location.reload", script)
        self.assertNotIn("fetch(", script)

    def test_dots_tooltips_and_non_navigating_day_selection_are_present(self):
        script = (STATIC / "js" / "calendar_drawer.js").read_text()

        self.assertIn("calendar-day-dots", script)
        self.assertIn("calendar-day-tooltip", script)
        self.assertIn("itemsByDate", script)
        self.assertIn("highlightCards(dateKey)", script)
        self.assertIn("dateItems.forEach", script)


if __name__ == "__main__":
    unittest.main()
