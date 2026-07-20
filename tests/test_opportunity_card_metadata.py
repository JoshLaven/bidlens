import unittest
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader


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
            user_vote=None,
            updated_since_import=False,
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
            watched=False,
            last_activity=None,
        )
        data.update(overrides)
        opp = SimpleNamespace(**data)
        return self.environment.get_template("_opp_card.html").module.opp_card(opp, view=view)

    def test_collapsed_card_uses_simplified_metadata_hierarchy(self):
        self.assertIn("opp-card-meta-line--collapsed", self.collapsed)
        self.assertIn("opp-card-agency", self.collapsed)
        self.assertIn("opp-card-metadata-row", self.collapsed)
        self.assertIn("opp-card-due-icon", self.collapsed)
        self.assertIn("primary_pursuit_lane.name", self.collapsed)
        self.assertIn("opp.updated_since_import", self.collapsed)
        self.assertIn("Updated since import", self.collapsed)
        self.assertNotIn("opportunity-type-pill", self.collapsed)
        self.assertNotIn("source-pill", self.collapsed)

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
        self.assertIn("opp.naics", self.details)
        self.assertIn("opp.normalized_opportunity_type", self.details)

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
