import unittest

from bidlens.routes.opportunities import _clean_solicitation_description


class OpportunityDetailInformationArchitectureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open("src/bidlens/templates/detail.html", encoding="utf-8") as source:
            cls.template = source.read()
        with open("src/bidlens/templates/_opportunity_sidebar.html", encoding="utf-8") as source:
            cls.sidebar_template = source.read()

    def test_folder_has_five_non_overlapping_tabs(self):
        self.assertEqual(self.template.count('data-detail-tab="'), 5)
        for tab in ("overview", "communication", "notes", "reference", "history"):
            self.assertIn(f'data-detail-tab="{tab}"', self.template)
            self.assertIn(f'data-detail-panel="{tab}"', self.template)
        for removed_tab in ("description", "identifiers", "crm"):
            self.assertNotIn(f'data-detail-tab="{removed_tab}"', self.template)
            self.assertNotIn(f'data-detail-panel="{removed_tab}"', self.template)

    def test_overview_uses_persistent_rail_and_focused_main_column(self):
        overview_start = self.template.index('data-detail-panel="overview"')
        communication_start = self.template.index('data-detail-panel="communication"')
        overview = self.template[overview_start:communication_start]
        for content in ("Description", "Opportunity Brief"):
            self.assertIn(content, overview)
        for duplicated_field in ("Opportunity characteristics", "Due Date", "NAICS"):
            self.assertNotIn(duplicated_field, overview)
        self.assertNotIn("Original Solicitation", overview)
        self.assertNotIn("detail-overview-rail", overview)
        self.assertIn('class="detail-description-folder detail-overview-content detail-overview-main"', overview)

    def test_overview_description_is_expandable_and_accessible(self):
        self.assertIn('aria-controls="overview-description-text"', self.template)
        self.assertIn('aria-expanded="false"', self.template)
        self.assertIn("toggleOverviewDescription(this)", self.template)
        self.assertIn("Show less", self.template)
        self.assertIn("Read more", self.template)

    def test_source_html_is_cleaned_with_paragraphs_preserved(self):
        value = "<p>Hello&nbsp;there</p><div>Second <span>paragraph</span><br>Next line</div>"
        self.assertEqual(
            _clean_solicitation_description(value),
            "Hello there\n\nSecond paragraph\nNext line",
        )

    def test_salesforce_and_email_rail_actions_are_safe(self):
        self.assertIn("{% if opportunity.salesforce_opportunity_url %}", self.sidebar_template)
        self.assertIn("View Opportunity &#8599;", self.sidebar_template)
        self.assertIn("Not linked", self.sidebar_template)
        self.assertIn("Link Opportunity &#8594;", self.sidebar_template)
        self.assertIn("linkOverviewSalesforce", self.template)
        self.assertIn('aria-label="Email this opportunity"', self.sidebar_template)
        self.assertIn("openOpportunityEmail", self.template)
        self.assertIn('aria-label="Copy solicitation number"', self.sidebar_template)

    def test_brief_empty_and_generated_states_have_distinct_visual_treatments(self):
        self.assertIn("{% if brief %}", self.template)
        self.assertIn('class="detail-description-section detail-description-section--brief overview-brief-card"', self.template)
        self.assertIn('class="detail-description-section overview-brief-action"', self.template)
        self.assertIn("Generate AI Brief", self.template)
        self.assertIn("Regenerate Brief", self.template)
        self.assertIn("AI-generated", self.template)
        self.assertIn("overview-document-count", self.template)

    def test_communication_uses_summary_and_collapsed_email_accordions(self):
        self.assertIn('id="communication-summary-heading">AI Summary', self.template)
        self.assertIn('class="communication-email-accordion"', self.template)
        self.assertNotIn('<details open class="communication-email-accordion"', self.template)
        self.assertIn("{{ message.body }}", self.template)
        accordion_start = self.template.index('<details class="communication-email-accordion">')
        summary_end = self.template.index("</summary>", accordion_start)
        self.assertNotIn("message.subject", self.template[accordion_start:summary_end])
        self.assertIn("<dt>Subject</dt>", self.template)
        self.assertNotIn("Documents</span>", self.template)

    def test_reference_combines_identifiers_source_and_salesforce_with_copy(self):
        reference_start = self.template.index('data-detail-panel="reference"')
        history_start = self.template.index('data-detail-panel="history"')
        reference = self.template[reference_start:history_start]
        for heading in ("Identifiers", "Source", "Salesforce"):
            self.assertIn(f">{heading}<", reference)
        self.assertIn('data-copy-value="{{ value }}"', reference)
        self.assertIn("Copy source URL", reference)

    def test_notes_and_history_have_explicit_responsibilities(self):
        self.assertIn("Personal working notes", self.template)
        self.assertIn("reminder, idea, follow-up, or question", self.template)
        self.assertIn("Solicitation History", self.template)
        self.assertIn("Amendments, updates, reposts, and source changes", self.template)

    def test_workspace_sidebar_is_present_across_every_tab(self):
        self.assertEqual(self.template.count("opportunity_sidebar("), 1)
        self.assertIn("opportunity_sidebar('workspace-team-interest-tooltip')", self.template)
        self.assertIn("detail-folder-workspace", self.template)
        self.assertIn("detail-folder-tab-content", self.template)
        self.assertNotIn("detail-shared-workspace", self.template)

    def test_history_source_action_is_in_the_card_header(self):
        header_start = self.template.index('class="detail-workspace-card-header"')
        history_start = self.template.index('data-detail-panel="history"')
        self.assertGreater(header_start, history_start)
        header = self.template[header_start:self.template.index("{% endif %}", header_start)]
        self.assertIn("opportunity-history-source-link", header)
        self.assertIn("source_label", header)


if __name__ == "__main__":
    unittest.main()
