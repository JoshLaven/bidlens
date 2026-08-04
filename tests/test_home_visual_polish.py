import datetime as dt
import unittest
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape


class HomeVisualPolishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        environment = Environment(
            loader=FileSystemLoader("src/bidlens/templates"),
            autoescape=select_autoescape(["html"]),
        )
        cls.template = environment.get_template("home.html")

    def _render(self, sections):
        return self.template.render(
            request=SimpleNamespace(),
            user=SimpleNamespace(name="Kendall Roy", email="kendall@example.test"),
            active_page="home",
            home={
                "snapshot_date": dt.date(2026, 8, 4),
                "snapshot_missing": False,
                "has_updates": True,
                "brief_points": ["This generated summary must not be rendered."],
                "feed_review": None,
                "shortlist_sections": sections,
            },
        )

    def test_daily_brief_uses_single_static_orientation_sentence(self):
        rendered = self._render([])

        self.assertIn("Here's what's waiting for you today.", rendered)
        self.assertNotIn("This generated summary must not be rendered.", rendered)

    def test_each_panel_hides_only_items_after_the_first_three(self):
        sections = []
        for key, title in (
            ("shortlist_deadlines", "Upcoming Due Dates"),
            ("shortlist_updates", "Opportunity Updates"),
        ):
            sections.append({
                "key": key,
                "title": title,
                "count": 5,
                "items": [
                    {
                        "title": f"{title} {index}",
                        "subtitle": f"Detail {index}",
                        "destination_url": f"/opportunity/{index}",
                    }
                    for index in range(1, 6)
                ],
            })

        rendered = self._render(sections)

        self.assertEqual(rendered.count('class="home-brief-show-more"'), 2)
        self.assertEqual(rendered.count("data-home-panel-item hidden"), 4)
        self.assertEqual(rendered.count("Show 2 more ↓"), 4)
        self.assertIn('aria-controls="home-brief-panel-shortlist_deadlines"', rendered)
        self.assertIn('aria-controls="home-brief-panel-shortlist_updates"', rendered)
        self.assertIn("button.closest('[data-home-shortlist-section]')", rendered)

    def test_responsive_styles_stack_shortlist_panels(self):
        css = Path("src/bidlens/static/css/styles.css").read_text()

        self.assertIn("grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));", css)
        self.assertIn("@media (max-width: 760px)", css)
        self.assertIn("grid-template-columns: 1fr;", css)

    def test_panels_render_subtle_semantic_icons(self):
        sections = [
            {"key": key, "title": title, "count": 1, "items": [{
                "title": "Opportunity", "subtitle": "Supporting detail",
                "destination_url": "/opportunity/1",
            }]}
            for key, title in (
                ("shortlist_deadlines", "Upcoming Due Dates"),
                ("shortlist_updates", "Opportunity Updates"),
                ("team_signals", "Team Activity"),
            )
        ]

        rendered = self._render(sections)

        self.assertIn("home-brief-detail-icon--deadline", rendered)
        self.assertIn("home-brief-detail-icon--update", rendered)
        self.assertIn("home-brief-detail-icon--team", rendered)
        self.assertEqual(rendered.count('class="home-brief-detail-icon '), 3)


if __name__ == "__main__":
    unittest.main()
