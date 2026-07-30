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
            (SimpleNamespace(id=10, title="NIH Opportunity", agency="NIH", response_deadline=date(2026, 7, 18)), False),
            (SimpleNamespace(id=11, title="No Deadline", agency="", response_deadline=None), False),
            (SimpleNamespace(id=12, title="CDC Opportunity", agency="CDC", response_deadline=date(2026, 7, 18)), False),
        ]

        items = _calendar_drawer_items(rows)

        self.assertEqual([item["id"] for item in items], [10, 12])
        self.assertEqual([item["deadline"] for item in items], ["2026-07-18", "2026-07-18"])
        self.assertEqual(items[0]["url"], "/opportunity/10?return_to=shortlist")
        self.assertEqual(items[0]["agency"], "NIH")

    def test_shortlist_renders_reusable_drawer_with_server_payload(self):
        shortlist = (TEMPLATES / "my_shortlist.html").read_text()
        component = (TEMPLATES / "_calendar_drawer.html").read_text()

        self.assertIn("calendar_drawer(calendar_items", shortlist)
        self.assertIn("data-calendar-drawer-toggle", component)
        self.assertIn("data-calendar-items", component)
        self.assertIn("data-calendar-grid", component)
        self.assertIn("data-calendar-selected-day", component)
        self.assertIn('aria-label="Open shortlist calendar"', component)
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
        self.assertNotIn("scrollTo", script)

    def test_dots_tooltips_and_non_navigating_day_selection_are_present(self):
        script = (STATIC / "js" / "calendar_drawer.js").read_text()

        self.assertIn("calendar-day-dots", script)
        self.assertIn("calendar-day-tooltip", script)
        self.assertIn("itemsByDate", script)
        self.assertIn("highlightCards(dateKey)", script)
        self.assertIn("dateItems.forEach", script)

    def test_today_and_selected_dates_have_distinct_accessible_states(self):
        script = (STATIC / "js" / "calendar_drawer.js").read_text()
        styles = (STATIC / "css" / "styles.css").read_text()

        self.assertIn("dateKey === isoDate(today)", script)
        self.assertIn("button.setAttribute('aria-current', 'date')", script)
        self.assertIn("button.setAttribute('aria-selected'", script)
        self.assertIn("calendar-day--selected:not(.calendar-day--today)", styles)
        self.assertIn(".calendar-day--today.calendar-day--selected > button", styles)
        self.assertIn("visibleMonth = new Date(today.getFullYear(), today.getMonth(), 1)", script)

    def test_tooltip_uses_fixed_collision_aware_viewport_positioning(self):
        script = (STATIC / "js" / "calendar_drawer.js").read_text()
        styles = (STATIC / "css" / "styles.css").read_text()

        self.assertIn("document.body.appendChild(floatingTooltip)", script)
        self.assertIn("window.innerWidth - tooltipRect.width - margin", script)
        self.assertIn("window.innerHeight - margin", script)
        self.assertIn("anchorRect.bottom + gap", script)
        self.assertIn("position: fixed", styles)

    def test_selected_day_renders_empty_single_and_multiple_deadline_content(self):
        script = (STATIC / "js" / "calendar_drawer.js").read_text()

        self.assertIn("itemsByDate.get(selectedDate) || []", script)
        self.assertIn("No shortlisted opportunities due on this date", script)
        self.assertIn("dateItems.forEach((item)", script)
        self.assertIn("link.href = item.url", script)
        self.assertIn("selectedDay.hidden = false", script)


if __name__ == "__main__":
    unittest.main()
