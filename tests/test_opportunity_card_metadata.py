import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

from bidlens.routes.opportunities import _datetime_is_after


class OpportunityCardMetadataTests(unittest.TestCase):
    def setUp(self):
        template = Path("src/bidlens/templates/_opp_card.html").read_text()
        self.collapsed, self.details = template.split("{# === EXPANDABLE DETAILS === #}", 1)
        self.environment = Environment(loader=FileSystemLoader("src/bidlens/templates"))

    def _render_card(self, title, view="feed", **overrides):
        data = dict(
            id=123,
            title=title,
            agency="test.agency",
            response_deadline=None,
            days_until_due=None,
            teammate_interest_users=[],
            pursuit_lanes=[],
            crm_pushed=False,
            crm_pushed_by_current_user=False,
            salesforce_opportunity_url=None,
            salesforce_opportunity_id=None,
            salesforce_action=None,
            preview_description="Preview",
            preview_has_sam_fallback=False,
            source="sam",
            normalized_opportunity_type="RFP",
            canonical_type="Contract",
            qualification_display=None,
            user_vote=None,
            updated_since_import=False,
            updated_since_shortlisted=False,
            team_interest_label="No team interest yet",
            pursue_count=0,
            source_url=None,
            sam_url=None,
            account_type=None,
            crm_pushed_by_label=None,
            set_aside=None,
            naics=None,
            naics_title=None,
            solicitation_number=None,
            source_record_id=None,
            external_source_key=None,
            govwin_staging_id=None,
            sam_notice_id=None,
            posted_date=None,
            date_shortlisted=None,
            watched=False,
            last_activity=None,
        )
        data.update(overrides)
        opp = SimpleNamespace(**data)
        return self.environment.get_template("_opp_card.html").module.opp_card(opp, view=view)

    def test_collapsed_card_uses_simplified_metadata_hierarchy(self):
        self.assertIn("opp-card-meta-line--collapsed", self.collapsed)
        self.assertIn("opp-card-agency", self.collapsed)
        self.assertIn("opp.agency_display", self.collapsed)
        self.assertNotIn("raw_agency", self.collapsed)
        self.assertIn("opp-card-metadata-row", self.collapsed)
        self.assertIn("opp-card-due-icon", self.collapsed)
        self.assertIn("primary_pursuit_lane.name", self.collapsed)
        self.assertNotIn("opp.updated_since_import", self.collapsed)
        self.assertNotIn("Updated since import", self.collapsed)
        self.assertIn("opp.updated_since_shortlisted", self.collapsed)
        self.assertIn("Updated since Shortlisted", self.collapsed)
        self.assertNotIn("opportunity-type-pill", self.collapsed)
        self.assertNotIn("source-pill", self.collapsed)

    def test_missing_canonical_stage_is_not_rendered_as_rfp(self):
        html = self._render_card(
            "Unclassified Grants.gov opportunity",
            view="user_archive",
            source="grants_gov",
            normalized_opportunity_type=None,
        )
        self.assertNotIn("<span class=\"opp-detail-value\">RFP</span>", html)

    def test_expanded_footer_contains_details_without_legacy_actions(self):
        footer = self.details.split('<div class="opp-detail-actions">', 1)[1].split("</div>", 1)[0]
        self.assertIn("Details &rarr;", footer)
        self.assertNotIn("Follow", footer)
        self.assertNotIn("showArchiveModal", footer)
        self.assertNotIn('/watch', footer)

    def test_lane_is_rendered_only_inline_without_redundant_matched_lanes_section(self):
        feed_html = self._render_card(
            "Lane opportunity",
            view="feed",
            pursuit_lanes=[{"name": "Health", "reasons": ["Agency match"]}],
        )
        shortlist_html = self._render_card(
            "Lane opportunity",
            view="my_shortlist",
            pursuit_lanes=[{"name": "Health", "reasons": ["Agency match"]}],
        )
        self.assertEqual(feed_html.count("pursuit-lane-pill"), 1)
        self.assertIn(">Health</span>", feed_html)
        self.assertNotIn("pursuit-lane-pill", shortlist_html)
        self.assertNotIn("Matched Lanes", feed_html)

    def test_titles_use_single_line_css_truncation_and_title_tooltip(self):
        self.assertNotIn("title_limit = 70", self.collapsed)
        self.assertNotIn("title_prefix = opp.title[:title_limit]", self.collapsed)
        self.assertNotIn("title_display", self.collapsed)
        self.assertNotIn("opp-card-title-wrap--truncated", self.collapsed)
        self.assertIn("opp-card-title-tooltip", self.collapsed)
        self.assertIn("aria-describedby=\"opp-title-tooltip-", self.collapsed)
        self.assertIn(">{{ opp.title }}</a>", self.collapsed)
        css = Path("src/bidlens/static/css/styles.css").read_text()
        self.assertIn("white-space: nowrap;", css)
        self.assertIn("overflow: hidden;", css)
        self.assertIn("text-overflow: ellipsis;", css)
        self.assertNotIn("-webkit-line-clamp: 2;", css)

    def test_title_rendering_preserves_full_title_for_css_truncation(self):
        short_title = "FY26 Women Business Center Renewal Announcement"
        exact_title = "A" * 70
        long_title = "Alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima"

        short_html = self._render_card(short_title)
        exact_html = self._render_card(exact_title)
        long_html = self._render_card(long_title)

        self.assertIn(f">{short_title}</a>", short_html)
        self.assertIn(f">{exact_title}</a>", exact_html)
        self.assertIn(f">{long_title}</a>", long_html)
        self.assertIn("opp-card-title-tooltip", long_html)
        self.assertIn(long_title, long_html)

    def test_information_preview_and_email_tooltips_are_distinct(self):
        self.assertIn("opp-preview-popover", self.collapsed)
        self.assertIn("loadPreview", self.collapsed)
        self.assertIn("opp-email-action", self.details)
        self.assertIn("opp-action-tooltip", self.details)
        self.assertIn("Email Opportunity", self.details)
        self.assertNotIn("opp-card-action-buttons", self.collapsed)
        self.assertNotIn("opp-card-collab-actions", self.collapsed)

    def test_card_accordion_uses_more_less_info_and_preserves_details_link(self):
        css = Path("src/bidlens/static/css/styles.css").read_text()

        self.assertIn("More Info", self.details)
        self.assertIn("Less Info", self.details)
        self.assertIn("Details &rarr;", self.details)
        self.assertIn("grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);", css)
        self.assertIn("justify-self: center;", css)
        self.assertIn("opp-card-more[open] .opp-card-more-label-text--collapsed", css)
        self.assertIn("opp-card-more[open] .opp-card-more-label-text--expanded", css)

    def test_information_icon_is_anchored_without_a_dedicated_column(self):
        css = Path("src/bidlens/static/css/styles.css").read_text()

        self.assertIn(".opp-card-title-group {\n  display: flex;", css)
        self.assertIn("width: 100%;", css)
        self.assertIn("flex: 1 1 auto;", css)
        self.assertIn("min-width: 0;", css)
        self.assertIn("max-width: 100%;", css)
        self.assertIn("flex-shrink: 0;", css)
        self.assertIn(".opp-preview {\n  position: relative;", css)
        self.assertNotIn("opp-card-action-buttons", self.collapsed)

    def test_information_preview_includes_dynamic_source_link(self):
        grants_html = self._render_card(
            "Grant opportunity",
            source="grants_gov",
            source_url="https://www.grants.gov/search-results-detail/example",
        )
        sam_html = self._render_card(
            "SAM opportunity",
            source="sam",
            sam_url="https://sam.gov/opp/example",
        )

        self.assertIn('href="https://www.grants.gov/search-results-detail/example"', grants_html)
        self.assertIn("View on Grants.gov ↗", grants_html)
        self.assertIn('href="https://sam.gov/opp/example"', sam_html)
        self.assertIn("View on SAM.gov ↗", sam_html)
        self.assertIn('target="_blank"', grants_html)
        self.assertIn('rel="noreferrer"', grants_html)
        base = Path("src/bidlens/templates/base.html").read_text()
        self.assertIn("function opportunitySourceLinkLabel(url)", base)
        self.assertIn("data.source_url || data.sam_url || ''", base)
        self.assertIn("View on Grants.gov ↗", base)

    def test_micro_polish_spacing_buttons_and_agency_are_consistent(self):
        css = Path("src/bidlens/static/css/styles.css").read_text()

        self.assertIn("max-width: 1040px;", css)
        self.assertIn("margin-inline: auto;", css)
        self.assertIn("padding: 9px 14px 7px 20px;", css)
        self.assertIn("margin: 6px 0 0;", css)
        self.assertIn("gap: 10px;", css)
        self.assertIn("min-width: 116px;", css)
        self.assertIn("min-height: 27px;", css)
        self.assertIn("display: inline-flex;", css)
        self.assertIn("justify-content: center;", css)
        self.assertIn("text-align: center;", css)
        self.assertIn("font-size: 0.78rem;", css)

    def test_my_shortlist_collapsed_card_does_not_render_extra_crm_dot_row(self):
        shortlist_html = self._render_card(
            "Shortlisted opportunity",
            view="my_shortlist",
            user_vote="PURSUE",
        )

        self.assertIn("&#10003; Interested", shortlist_html)
        self.assertNotIn("opp-card-crm-inline", self.collapsed)
        self.assertNotIn("opp-card-crm-inline", shortlist_html)
        self.assertNotIn('content: "•";', Path("src/bidlens/static/css/styles.css").read_text())
        self.assertNotIn("&bull;", shortlist_html)

    def test_my_shortlist_interested_is_a_static_status(self):
        shortlist_html = self._render_card(
            "Shortlisted opportunity",
            view="my_shortlist",
            user_vote="PURSUE",
        )

        self.assertIn('class="btn btn-sm opp-card-button opp-card-button--primary btn-success opp-card-interest-status"', shortlist_html)
        self.assertIn('role="status"', shortlist_html)
        self.assertIn("&#10003; Interested", shortlist_html)
        self.assertNotIn('data-vote-button="PURSUE"', shortlist_html)
        self.assertNotIn("voteOpp('123', 'PURSUE')", shortlist_html)
        self.assertNotIn("<button", shortlist_html.split("&#10003; Interested", 1)[0].rsplit('<div class="opp-card-actions">', 1)[1])
        self.assertIn('data-vote-button="PASS"', shortlist_html)
        self.assertIn("voteOpp('123', 'PASS')", shortlist_html)

    def test_feed_interested_remains_an_action(self):
        feed_html = self._render_card(
            "Feed opportunity",
            view="feed",
            user_vote=None,
        )

        self.assertIn('data-vote-button="PURSUE"', feed_html)
        self.assertIn("voteOpp('123', 'PURSUE')", feed_html)
        self.assertNotIn("opp-card-interest-status", feed_html)

    def test_feed_and_my_shortlist_use_same_collapsed_action_wrapper(self):
        feed_html = self._render_card("Shared opportunity", view="feed", user_vote="PURSUE")
        shortlist_html = self._render_card("Shared opportunity", view="my_shortlist", user_vote="PURSUE")
        css = Path("src/bidlens/static/css/styles.css").read_text()

        self.assertIn('<div class="opp-card-actions-secondary">', feed_html)
        self.assertIn('<div class="opp-card-actions-secondary">', shortlist_html)
        self.assertNotIn("opp-card-actions-secondary--feed", feed_html)
        self.assertNotIn("opp-card-actions-secondary--feed", shortlist_html)
        self.assertNotIn("opp-card-actions-secondary--feed", css)
        self.assertIn(".opp-card-actions-secondary {\n  display: inline-flex;", css)
        self.assertIn("justify-content: flex-end;", css)

    def test_feed_and_my_shortlist_use_same_agency_metadata_spacing_rule(self):
        feed_html = self._render_card("Shared opportunity", view="feed", user_vote="PURSUE")
        shortlist_html = self._render_card("Shared opportunity", view="my_shortlist", user_vote="PURSUE")
        css = Path("src/bidlens/static/css/styles.css").read_text()

        feed_meta = feed_html[
            feed_html.index('<div class="opp-card-meta-line'):
            feed_html.index('<div class="opp-preview-inline')
        ]
        shortlist_meta = shortlist_html[
            shortlist_html.index('<div class="opp-card-meta-line'):
            shortlist_html.index('<div class="opp-preview-inline')
        ]
        self.assertEqual(feed_meta, shortlist_meta)
        self.assertIn(".opp-card-meta-line--collapsed {\n  align-items: flex-start;\n  flex-direction: column;\n  gap: 4px;", css)
        self.assertIn(
            '.opp-card:is([data-card-view="feed"], [data-card-view="my_shortlist"], [data-card-view="triage"], [data-card-view="user_archive"]) .opp-card-meta-line {\n  margin: 6px 0 0;',
            css,
        )
        self.assertNotIn('.opp-card[data-card-view="feed"] .opp-card-meta-line', css)

    def test_secondary_metadata_remains_in_details(self):
        self.assertNotIn("opp.solicitation_number", self.collapsed)
        self.assertNotIn("opp.naics", self.collapsed)
        self.assertIn("opp.solicitation_number", self.details)
        self.assertNotIn("opp.naics", self.details)
        self.assertIn("opp.canonical_type", self.details)
        self.assertIn("qualification_row(opp)", self.details)
        self.assertIn("opp.normalized_opportunity_type", self.details)

    def test_feed_body_uses_canonical_information_architecture(self):
        html = self._render_card(
            "Feed IA",
            view="feed",
            canonical_type="Contract",
            posted_date=datetime(2026, 8, 1),
            solicitation_number="RFP-100",
            source_record_id="source-100",
            external_source_key="sam:source-100",
            salesforce_opportunity_id="006xx",
        )
        body = html.split('<div class="opp-card-expanded">', 1)[1]

        for label in ("Type", "Stage", "Posted", "Solicitation Number"):
            self.assertIn(f">{label}</span>", body)
        self.assertNotIn('class="opp-detail-label">Salesforce', body)
        for removed in ("Source Record ID", "External Key", "CRM Activity", "Account Type", "Recent Changes"):
            self.assertNotIn(f">{removed}</span>", body)

    def test_unclassified_type_uses_concise_display_label(self):
        html = self._render_card("Unclassified type", canonical_type=None)

        self.assertIn(">Unclassified</span>", html)
        self.assertNotIn("Not yet classified", html)

    def test_linked_salesforce_metadata_uses_accessible_external_action(self):
        for view in ("feed", "my_shortlist", "user_archive"):
            with self.subTest(view=view):
                html = self._render_card(
                    "Salesforce action",
                    view=view,
                    salesforce_opportunity_id="006linked",
                    salesforce_opportunity_url="https://salesforce.example/006linked",
                    salesforce_action="created",
                )
                body = html.split('<div class="opp-card-expanded">', 1)[1]
                self.assertNotIn('class="opp-detail-label">Salesforce', body)
                self.assertIn('<span class="sr-only">Salesforce</span>', body)
                self.assertEqual(body.count("Open in Salesforce"), 1)
                self.assertIn('href="https://salesforce.example/006linked"', body)
                self.assertIn('target="_blank"', body)
                self.assertIn('rel="noreferrer"', body)
                self.assertNotIn("Created in Salesforce", body)

    def test_salesforce_link_count_respects_queue_and_linkage(self):
        for view, expected in (
            ("feed", 1),
            ("my_shortlist", 1),
            ("user_archive", 1),
            ("triage", 0),
        ):
            with self.subTest(view=view):
                linked = self._render_card(
                    "Linked Salesforce opportunity",
                    view=view,
                    salesforce_opportunity_id="006linked",
                    salesforce_opportunity_url="https://salesforce.example/006linked",
                )
                self.assertEqual(linked.count("Open in Salesforce"), expected)

        for view in ("feed", "my_shortlist", "user_archive", "triage"):
            with self.subTest(view=view):
                unlinked = self._render_card(
                    "Unlinked Salesforce opportunity",
                    view=view,
                    salesforce_opportunity_id=None,
                    salesforce_opportunity_url=None,
                )
                self.assertNotIn("Open in Salesforce", unlinked)

    def test_my_shortlist_body_and_update_indicator_use_shortlist_context(self):
        html = self._render_card(
            "Shortlist IA",
            view="my_shortlist",
            user_vote="PURSUE",
            updated_since_import=True,
            updated_since_shortlisted=True,
            date_shortlisted=datetime(2026, 8, 2, tzinfo=timezone.utc),
            posted_date=datetime(2026, 8, 1),
            solicitation_number="RFP-200",
            salesforce_opportunity_id="006yy",
            salesforce_opportunity_url="https://salesforce.example/006yy",
            source_record_id="source-200",
            external_source_key="sam:source-200",
        )
        body = html.split('<div class="opp-card-expanded">', 1)[1]

        self.assertIn("Updated since Shortlisted", html)
        self.assertNotIn("Updated since import", html)
        for label in ("Type", "Stage", "Posted", "Solicitation Number", "Date Shortlisted"):
            self.assertIn(f">{label}</span>", body)
        self.assertNotIn('class="opp-detail-label">Salesforce', body)
        self.assertIn("Open in Salesforce", body)
        for removed in ("CRM Activity", "Source Record ID", "External Key"):
            self.assertNotIn(f">{removed}</span>", body)

    def test_shortlist_update_requires_an_event_after_date_shortlisted(self):
        shortlisted = datetime(2026, 8, 2, 9, tzinfo=timezone.utc)

        self.assertFalse(_datetime_is_after(None, shortlisted))
        self.assertFalse(_datetime_is_after(datetime(2026, 8, 2, 8), shortlisted))
        self.assertFalse(_datetime_is_after(datetime(2026, 8, 2, 9), shortlisted))
        self.assertTrue(_datetime_is_after(datetime(2026, 8, 2, 10), shortlisted))

    def test_triage_body_keeps_source_context_without_crm_metadata(self):
        html = self._render_card(
            "Triage IA",
            view="triage",
            posted_date=datetime(2026, 8, 1),
            solicitation_number="RFP-300",
            source_record_id="source-300",
            external_source_key="sam:source-300",
            salesforce_opportunity_url="https://salesforce.example/006zz",
            crm_pushed=True,
            crm_pushed_by_label="Admin",
        )
        body = html.split('<div class="opp-card-expanded">', 1)[1]

        for label in ("Type", "Stage", "Posted", "Solicitation Number", "Source", "Source Record ID"):
            self.assertIn(f">{label}</span>", body)
        for removed in ("External Key", "Salesforce", "CRM Activity"):
            self.assertNotIn(f">{removed}</span>", body)

    def test_archive_body_mirrors_feed_metadata(self):
        html = self._render_card(
            "Archive IA",
            view="user_archive",
            posted_date=datetime(2026, 8, 1),
            solicitation_number="RFP-400",
            salesforce_opportunity_id="006aa",
            source_record_id="source-400",
            external_source_key="sam:source-400",
        )
        body = html.split('<div class="opp-card-expanded">', 1)[1]

        for label in ("Type", "Stage", "Posted", "Solicitation Number"):
            self.assertIn(f">{label}</span>", body)
        self.assertNotIn('class="opp-detail-label">Salesforce', body)
        for removed in ("Source Record ID", "External Key", "CRM Activity"):
            self.assertNotIn(f">{removed}</span>", body)

    def test_card_css_keeps_hover_cards_above_neighbors(self):
        css = Path("src/bidlens/static/css/styles.css").read_text()

        self.assertIn(".opp-card:hover", css)
        self.assertIn("z-index: 30;", css)
        self.assertIn(".opp-card:focus-within", css)
        self.assertIn("z-index: 35;", css)
        self.assertIn(".opp-preview-popover", css)
        self.assertIn("z-index: 230;", css)
        self.assertIn(".opp-card-title-tooltip", css)
        self.assertIn(".opp-action-tooltip", css)

    def test_green_accent_restores_shared_feed_shortlist_triage_card_selector(self):
        css = Path("src/bidlens/static/css/styles.css").read_text()
        base = Path("src/bidlens/templates/base.html").read_text()
        feed = Path("src/bidlens/templates/feed.html").read_text()
        shortlist = Path("src/bidlens/templates/my_shortlist.html").read_text()
        triage = Path("src/bidlens/templates/triage.html").read_text()
        expected_views = (
            '[data-card-view="feed"]',
            '[data-card-view="my_shortlist"]',
            '[data-card-view="triage"]',
        )
        shared_selector = (
            '.opp-card:is([data-card-view="feed"], [data-card-view="my_shortlist"], '
            '[data-card-view="triage"])'
        )

        self.assertIn(f"{shared_selector}::before", css)
        self.assertIn(f"{shared_selector}:is(:hover, :focus-within)::before", css)
        rail_rule = css[css.index(f"{shared_selector}::before") : css.index(f"{shared_selector}:is(:hover, :focus-within)::before")]
        self.assertIn('content: "";', rail_rule)
        self.assertIn("position: absolute;", rail_rule)
        self.assertIn("width: 3px;", rail_rule)
        self.assertIn("background: rgba(40, 139, 92, 0.68);", rail_rule)
        for view in expected_views:
            self.assertIn(view, css)
        self.assertNotIn("function syncOpportunityCardSelectedStates", base)
        self.assertNotIn("syncOpportunityCardSelectedStates", feed)
        self.assertNotIn("syncOpportunityCardSelectedStates", shortlist)
        self.assertNotIn("syncOpportunityCardSelectedStates", triage)
        self.assertNotIn("new MutationObserver", base)
        self.assertNotIn("opp-card--selected", css)
        self.assertNotIn(":has(.opp-card-select input:checked)", css)


if __name__ == "__main__":
    unittest.main()
