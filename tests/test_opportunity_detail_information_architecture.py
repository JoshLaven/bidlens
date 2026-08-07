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
        for content in ("Description", "Get Up To Speed"):
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
        self.assertNotIn("Link Opportunity &#8594;", self.sidebar_template)
        linked_branch = self.sidebar_template[
            self.sidebar_template.index("{% if opportunity.salesforce_opportunity_url %}"):
            self.sidebar_template.index(
                "{% else %}",
                self.sidebar_template.index("{% if opportunity.salesforce_opportunity_url %}"),
            )
        ]
        self.assertIn("View Opportunity &#8599;", linked_branch)
        self.assertIn('aria-label="Email this opportunity"', self.sidebar_template)
        self.assertIn("openOpportunityEmail", self.template)
        self.assertIn('aria-label="Copy solicitation number"', self.sidebar_template)

    def test_get_up_to_speed_replaces_the_legacy_brief_presentation(self):
        self.assertIn('class="detail-description-section guts-card"', self.template)
        self.assertIn('id="guts-heading">Get Up To Speed', self.template)
        self.assertIn("A holistic summary of this opportunity", self.template)
        self.assertIn("based on the latest communications and notes.", self.template)
        self.assertIn("AI Brief", self.template)
        self.assertNotIn(">Opportunity Brief<", self.template)
        self.assertNotIn(">Generate AI Brief<", self.template)

    def test_get_up_to_speed_uses_team_summary_and_recent_developments(self):
        for heading in ("Team Summary", "Recent Developments"):
            self.assertIn(heading, self.template)
        for excluded_heading in (
            "Overall Status",
            "Official Updates",
            "Internal Activity",
            "Risks / Watch Items",
            "Suggested Next Steps",
        ):
            self.assertNotIn(excluded_heading, self.template)
        for empty_state in ("No Team Summary has been generated yet.", "No recent developments identified."):
            self.assertIn(empty_state, self.template)

    def test_get_up_to_speed_uses_server_side_team_summary_generation(self):
        self.assertIn(
            'action="/opportunity/{{ opportunity.id }}/communication-summary"',
            self.template,
        )
        self.assertIn('name="csrf_token" value="{{ communication_summary_csrf_token }}"', self.template)
        self.assertIn('name="return_tab" value="overview"', self.template)
        self.assertIn("communication_summary.status == 'ready'", self.template)
        self.assertNotIn('id="generate-brief-button"', self.template)
        self.assertNotIn("generateBrief(", self.template)
        self.assertNotIn("/generate-guts", self.template)
        self.assertIn("overview-document-count", self.template)

    def test_get_up_to_speed_renders_current_team_summary_and_developments(self):
        self.assertIn("{{ communication_summary.current_status }}", self.template)
        self.assertIn("for statement in recent_developments", self.template)
        self.assertIn('data-guts-statement-id="{{ statement.persisted_statement_id }}"', self.template)
        self.assertIn('data-guts-statement-key="{{ statement.statement_key }}"', self.template)
        self.assertIn("{{ statement.text }}", self.template)
        for legacy_collection in (
            "guts_presentation.overall_status",
            "guts_presentation.recent_developments",
            "guts_presentation.internal_activity",
        ):
            self.assertNotIn(legacy_collection, self.template)

    def test_communication_uses_only_timeline_and_collapsed_email_accordions(self):
        communication = self.template[
            self.template.index('data-detail-panel="communication"'):
            self.template.index('data-detail-panel="notes"')
        ]
        self.assertNotIn("Team Summary", communication)
        self.assertNotIn('id="communication-summary-heading"', communication)
        self.assertIn('id="communication-timeline-heading"', communication)
        self.assertIn('class="communication-email-accordion"', communication)
        self.assertNotIn('<details open class="communication-email-accordion"', communication)
        self.assertIn("{{ message.body }}", communication)
        accordion_start = communication.index('<details class="communication-email-accordion">')
        summary_end = communication.index("</summary>", accordion_start)
        self.assertNotIn("message.subject", communication[accordion_start:summary_end])
        self.assertIn("<dt>Subject</dt>", communication)
        self.assertNotIn("Documents</span>", communication)

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
        self.assertIn("detail-folder-inspector", self.template)
        self.assertIn('class="detail-folder"', self.template)
        self.assertIn("detail-folder-tab-content", self.template)
        self.assertNotIn("detail-shared-workspace", self.template)

    def test_folder_tabs_live_with_the_content_beside_the_persistent_inspector(self):
        workspace_start = self.template.index('class="detail-primary detail-folder-workspace"')
        inspector_start = self.template.index('class="detail-folder-inspector"', workspace_start)
        folder_start = self.template.index('class="detail-folder"', inspector_start)
        nav_start = self.template.index('class="detail-section-nav"', folder_start)
        content_start = self.template.index('class="detail-folder-tab-content"', nav_start)
        self.assertLess(inspector_start, folder_start)
        self.assertLess(folder_start, nav_start)
        self.assertLess(nav_start, content_start)

    def test_persistent_inspector_uses_current_identity_and_metadata_groups(self):
        self.assertNotIn("detail-context-row--client", self.sidebar_template)
        self.assertNotIn(">Client<", self.sidebar_template)
        self.assertIn('class="detail-context-row detail-context-row--source"', self.sidebar_template)
        self.assertIn("View Source &#8599;", self.sidebar_template)
        self.assertIn('class="detail-inspector-metadata"', self.sidebar_template)
        metadata_start = self.sidebar_template.index('class="detail-inspector-metadata"')
        metadata_end = self.sidebar_template.index("</dl>", metadata_start)
        metadata = self.sidebar_template[metadata_start:metadata_end]
        self.assertIn(">Created<", metadata)
        self.assertIn(">Last updated<", metadata)
        self.assertNotIn(">Source<", metadata)

    def test_history_source_action_is_in_the_card_header(self):
        header_start = self.template.index('class="detail-workspace-card-header"')
        history_start = self.template.index('data-detail-panel="history"')
        self.assertGreater(header_start, history_start)
        header = self.template[header_start:self.template.index("{% endif %}", header_start)]
        self.assertIn("opportunity-history-source-link", header)
        self.assertIn("source_label", header)


if __name__ == "__main__":
    unittest.main()
