import unittest
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader


TEMPLATES = Path("src/bidlens/templates")


class QueueFilterPopoverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        environment = Environment(loader=FileSystemLoader(TEMPLATES))
        environment.filters["urlencode"] = lambda value: value
        cls.toolbar = environment.get_template("_queue_layout.html").module.queue_toolbar
        cls.source_options = [
            SimpleNamespace(value="sam", label="SAM.gov"),
            SimpleNamespace(value="grants", label="Grants.gov"),
            SimpleNamespace(value="govwin", label="GovWin"),
        ]
        cls.lanes = [SimpleNamespace(id=1, name="Health"), SimpleNamespace(id=2, name="Data")]

    def render(self, **overrides):
        values = {
            "route": "/",
            "id_prefix": "feed",
            "sort": "imported",
            "direction": "desc",
            "q": "",
            "active_lanes": self.lanes,
            "stages_value": "Forecast,RFI,RFP",
            "sources_value": "sam,grants,govwin",
            "selected_stages": ["Forecast", "RFI", "RFP"],
            "source_options": self.source_options,
            "selected_sources": ["sam", "grants", "govwin"],
        }
        values.update(overrides)
        return self.toolbar(**values)

    def test_popover_is_hidden_and_accessibly_connected_by_default(self):
        html = self.render()

        self.assertIn('aria-expanded="false"', html)
        self.assertIn('aria-controls="feed-filters-popover"', html)
        self.assertIn('id="feed-filters-popover"', html)
        self.assertIn("data-queue-filter-panel", html)
        self.assertIn("hidden", html)

    def test_active_count_counts_only_constraining_selections(self):
        html = self.render(
            selected_stages=["RFP"],
            selected_sources=["sam"],
            lane_id="2",
            stages_value="RFP",
            sources_value="sam",
        )

        self.assertIn("Filters (3)", html)

    def test_selecting_all_stage_and_source_options_is_not_counted(self):
        html = self.render()

        self.assertIn(">Filters<", html)
        self.assertNotIn("Filters (", html)

    def test_existing_chip_forms_preserve_query_state(self):
        html = self.render(q="cancer", lane_id="2", sort="lane", direction="asc")

        self.assertIn('name="q" value="cancer"', html)
        self.assertIn('name="sort" value="lane"', html)
        self.assertIn('name="direction" value="asc"', html)
        self.assertIn('name="lane_id" value="2"', html)

    def test_popover_supports_toggle_outside_click_and_escape(self):
        html = self.render()

        self.assertIn("trigger.addEventListener('click'", html)
        self.assertIn("document.addEventListener('pointerdown'", html)
        self.assertIn("event.key !== 'Escape'", html)
        self.assertIn("trigger.focus({preventScroll: true})", html)

    def test_filter_chip_navigation_preserves_the_open_popover(self):
        html = self.render()

        self.assertIn("preserveOpenOnPageHide = true", html)
        self.assertIn("rememberOpen(true)", html)
        self.assertIn("[data-stage-filter-chip], [data-source-filter-chip], [data-lane-filter-chip]", html)
        self.assertIn("window.sessionStorage.getItem(storageKey) === 'true'", html)
        self.assertIn("setOpen(true, {persist: false})", html)

    def test_non_filter_navigation_does_not_leave_the_popover_open(self):
        html = self.render()

        self.assertIn("window.addEventListener('pagehide'", html)
        self.assertIn("if (!preserveOpenOnPageHide) rememberOpen(false)", html)
        self.assertIn("else window.sessionStorage.removeItem(storageKey)", html)

    def test_all_queue_templates_use_the_shared_component(self):
        templates = {
            name: (TEMPLATES / name).read_text()
            for name in ("feed.html", "my_shortlist.html", "triage.html", "archive.html")
        }

        for name, template in templates.items():
            with self.subTest(name=name):
                self.assertIn("queue_toolbar(", template)

        self.assertIn("show_filters=true", templates["feed.html"])
        self.assertIn("show_filters=true", templates["my_shortlist.html"])
        self.assertIn("none, true, false", templates["archive.html"])
        self.assertIn("none, true, false", templates["triage.html"])

    def test_member_templates_keep_only_existing_filter_capabilities(self):
        feed = (TEMPLATES / "feed.html").read_text()
        shortlist = (TEMPLATES / "my_shortlist.html").read_text()
        archive = (TEMPLATES / "archive.html").read_text()

        for template in (feed, shortlist):
            self.assertIn("source_options=source_options if user.current_role == 'admin' else none", template)
            self.assertIn("active_lanes", template)
        self.assertIn("active_lanes if user.current_role == 'admin' else none", archive)


if __name__ == "__main__":
    unittest.main()
