import unittest

from jinja2 import Environment, FileSystemLoader, select_autoescape

from bidlens.routes.opportunities import opportunity_back_navigation


class BackNavigationTests(unittest.TestCase):
    def test_opportunity_contexts_are_allowlisted(self):
        expected = {
            "feed": ("Feed", "/"),
            "shortlist": ("My Shortlist", "/my-shortlist"),
            "archive": ("Archive", "/archive"),
            "past_due": ("Past Due Opportunities", "/past-due-outcomes"),
            "triage": ("Triage", "/triage"),
        }
        for value, destination in expected.items():
            self.assertEqual(opportunity_back_navigation(value), destination)

    def test_unknown_or_missing_context_falls_back_to_feed(self):
        self.assertEqual(opportunity_back_navigation(None), ("Feed", "/"))
        self.assertEqual(opportunity_back_navigation("https://evil.example"), ("Feed", "/"))
        self.assertEqual(opportunity_back_navigation("../../admin"), ("Feed", "/"))

    def test_shared_component_has_one_visual_contract(self):
        env = Environment(
            loader=FileSystemLoader("src/bidlens/templates"),
            autoescape=select_autoescape(),
        )
        template = env.from_string(
            '{% from "_back_navigation.html" import back_navigation %}'
            '{{ back_navigation("Integrations", "/integrations") }}'
        )
        html = template.render()
        self.assertIn('class="back-navigation"', html)
        self.assertIn('href="/integrations"', html)
        self.assertIn("← Back to Integrations", html)
        self.assertNotIn("btn", html)

    def test_shared_component_falls_back_when_values_are_missing_or_falsey(self):
        env = Environment(loader=FileSystemLoader("src/bidlens/templates"), autoescape=select_autoescape())
        template = env.from_string(
            '{% from "_back_navigation.html" import back_navigation %}'
            '{{ back_navigation(missing_label, missing_url) }} {{ back_navigation("", none) }}'
        )
        html = template.render()
        self.assertEqual(html.count('href="/"'), 2)
        self.assertEqual(html.count("← Back to Feed"), 2)

    def test_known_opportunity_entry_points_supply_context(self):
        with open("src/bidlens/templates/_opp_card.html", encoding="utf-8") as source:
            cards = source.read()
        with open("src/bidlens/templates/past_due_outcomes.html", encoding="utf-8") as source:
            past_due = source.read()
        self.assertIn("?return_to={{ return_context }}", cards)
        self.assertIn("?return_to=past_due", past_due)

    def test_opportunity_sidebar_uses_the_validated_origin(self):
        with open("src/bidlens/templates/base.html", encoding="utf-8") as source:
            template = source.read()
        with open("src/bidlens/routes/opportunities.py", encoding="utf-8") as source:
            route_source = source.read()
        self.assertIn("opportunity_origin_active_page|default('feed', true)", template)
        self.assertIn('"opportunity_origin_active_page": back_destination.active_page', route_source)

    def test_integration_pages_use_shared_component(self):
        for filename in ("salesforce_configuration.html", "microsoft_connection.html", "govwin_integration.html"):
            with open(f"src/bidlens/templates/{filename}", encoding="utf-8") as source:
                template = source.read()
            self.assertIn('_back_navigation.html', template)
            self.assertIn("back_navigation(", template)


if __name__ == "__main__":
    unittest.main()
