import unittest
from pathlib import Path


ROOT = Path("src/bidlens")


class EmailWorkflowConsistencyTests(unittest.TestCase):
    def test_opportunity_folder_uses_shared_modal_entry_point(self):
        detail = (ROOT / "templates/detail.html").read_text()

        self.assertIn("openOpportunityEmail({ id:", detail)
        self.assertNotIn('href="/opportunity/{{ opportunity.id }}/conversation/new', detail)
        self.assertNotIn("opportunity-compose-form", detail)

    def test_modal_close_control_closes_without_navigation_or_submit(self):
        base = (ROOT / "templates/base.html").read_text()

        self.assertIn("data-opportunity-email-close", base)
        self.assertIn("window.closeOpportunityEmailDialog();", base)
        self.assertIn("if (dialog.open) dialog.close();", base)
        self.assertIn("event.data === 'bidlens:close-opportunity-email'", base)

    def test_embedded_cancel_uses_modal_close_message(self):
        compose = (ROOT / "templates/_opportunity_email_compose_content.html").read_text()

        self.assertIn("window.parent.postMessage('bidlens:close-opportunity-email'", compose)
        self.assertIn('<button type="button" class="btn btn-outline-secondary"', compose)
        self.assertNotIn('target="_top">Cancel</a>', compose)

    def test_only_shared_compose_partial_contains_compose_form(self):
        templates = list((ROOT / "templates").glob("*.html"))
        form_templates = [path.name for path in templates if "opportunity-compose-form" in path.read_text()]

        self.assertEqual(form_templates, ["_opportunity_email_compose_content.html"])


if __name__ == "__main__":
    unittest.main()
