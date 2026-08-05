import datetime as dt
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import (
    Opportunity,
    OpportunityCommunicationMessage,
    OpportunityConversation,
    OpportunityHistoryEvent,
    OpportunityNote,
    Organization,
    User,
    Workspace,
)
from bidlens.services.shortlist_activity import shortlist_recent_activity


class ShortlistRecentActivityTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Activity Org", slug="activity-org")
        self.other_org = Organization(name="Other Org", slug="other-activity-org")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.workspace = Workspace(organization_id=self.org.id, name="Activity", slug="activity")
        self.other_workspace = Workspace(
            organization_id=self.other_org.id, name="Other Activity", slug="other-activity"
        )
        self.author = User(name="Kendall Roy", email="kendall@example.com", organization_id=self.org.id)
        self.db.add_all([self.workspace, self.other_workspace, self.author])
        self.db.flush()
        self.opportunity = self._opportunity(self.org.id, "Visible opportunity")
        self.other_opportunity = self._opportunity(self.other_org.id, "Other opportunity")
        self.db.add_all([self.opportunity, self.other_opportunity])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def _opportunity(organization_id, title):
        return Opportunity(
            organization_id=organization_id,
            source="sam",
            source_record_id=f"{organization_id}-{title}",
            title=title,
            agency="Agency",
            opportunity_type="Solicitation",
            posted_date=dt.date(2026, 7, 1),
            response_deadline=dt.date(2026, 8, 20),
        )

    def _conversation(self, workspace, opportunity, suffix):
        conversation = OpportunityConversation(
            workspace_id=workspace.id,
            opportunity_id=opportunity.id,
            provider="microsoft",
            external_conversation_id=f"thread-{suffix}",
            subject=f"Thread {suffix}",
        )
        self.db.add(conversation)
        self.db.flush()
        return conversation

    def test_latest_canonical_activity_is_compact_and_tenant_scoped(self):
        earlier = dt.datetime(2026, 8, 1, 10, 0)
        latest = dt.datetime(2026, 8, 3, 12, 0)
        self.db.add_all([
            OpportunityHistoryEvent(
                organization_id=self.org.id,
                opportunity_id=self.opportunity.id,
                event_type="source_updated",
                source="sam",
                occurred_at=earlier,
                event_data={"changed_fields": ["description"]},
            ),
            OpportunityHistoryEvent(
                organization_id=self.org.id,
                opportunity_id=self.opportunity.id,
                event_type="source_updated",
                source="sam",
                occurred_at=latest,
                event_data={"changed_fields": ["response_deadline"]},
            ),
            OpportunityHistoryEvent(
                organization_id=self.other_org.id,
                opportunity_id=self.opportunity.id,
                event_type="source_updated",
                source="grants_gov",
                occurred_at=latest + dt.timedelta(days=1),
                event_data={"changed_fields": ["description"]},
            ),
        ])
        conversation = self._conversation(self.workspace, self.opportunity, "visible")
        other_conversation = self._conversation(self.other_workspace, self.opportunity, "other")
        self.db.add_all([
            OpportunityCommunicationMessage(
                workspace_id=self.workspace.id,
                opportunity_id=self.opportunity.id,
                conversation_id=conversation.id,
                provider="microsoft",
                direction="inbound",
                provider_message_id="older",
                sender_display_name="Older Sender",
                subject="Older subject",
                body="PRIVATE OLDER BODY",
                provider_timestamp=earlier,
            ),
            OpportunityCommunicationMessage(
                workspace_id=self.workspace.id,
                opportunity_id=self.opportunity.id,
                conversation_id=conversation.id,
                provider="microsoft",
                direction="inbound",
                provider_message_id="latest",
                sender_display_name="Latest Sender",
                subject="Latest safe subject",
                body="PRIVATE LATEST BODY",
                provider_timestamp=latest,
            ),
            OpportunityCommunicationMessage(
                workspace_id=self.other_workspace.id,
                opportunity_id=self.opportunity.id,
                conversation_id=other_conversation.id,
                provider="microsoft",
                direction="inbound",
                provider_message_id="cross-org",
                sender_display_name="Cross Org Sender",
                subject="Cross-org subject",
                body="PRIVATE CROSS ORG BODY",
                provider_timestamp=latest + dt.timedelta(days=1),
            ),
            OpportunityNote(
                org_id=self.org.id,
                opportunity_id=self.opportunity.id,
                user_id=self.author.id,
                body="Older note",
                created_at=earlier,
            ),
            OpportunityNote(
                org_id=self.org.id,
                opportunity_id=self.opportunity.id,
                user_id=self.author.id,
                body="Latest note " + ("detail " * 40),
                created_at=latest,
            ),
            OpportunityNote(
                org_id=self.other_org.id,
                opportunity_id=self.opportunity.id,
                user_id=self.author.id,
                body="CROSS ORGANIZATION NOTE",
                created_at=latest + dt.timedelta(days=1),
            ),
        ])
        self.db.commit()

        activity = shortlist_recent_activity(
            self.db, organization_id=self.org.id, opportunities=[self.opportunity]
        )[str(self.opportunity.id)]

        self.assertEqual(activity["official_update"]["label"], "Due Date Changed")
        self.assertEqual(activity["official_update"]["source"], "SAM.gov")
        self.assertEqual(activity["communication"]["person"], "Latest Sender")
        self.assertEqual(activity["communication"]["preview"], "Latest safe subject")
        self.assertEqual(activity["note"]["author"], "Kendall Roy")
        self.assertLessEqual(len(activity["note"]["preview"]), 140)
        serialized = repr(activity)
        for excluded in (
            "PRIVATE LATEST BODY",
            "PRIVATE CROSS ORG BODY",
            "CROSS ORGANIZATION NOTE",
            "guts",
            "manifest",
        ):
            self.assertNotIn(excluded, serialized.casefold() if excluded.islower() else serialized)

    def test_missing_categories_and_empty_opportunity_list_are_safe(self):
        activity = shortlist_recent_activity(
            self.db, organization_id=self.org.id, opportunities=[self.opportunity]
        )[str(self.opportunity.id)]
        self.assertIsNone(activity["official_update"])
        self.assertIsNone(activity["communication"])
        self.assertIsNone(activity["note"])
        self.assertEqual(
            shortlist_recent_activity(self.db, organization_id=self.org.id, opportunities=[]),
            {},
        )

    def test_rail_reuses_shared_companion_shell_and_supports_hover_and_focus(self):
        shortlist = Path("src/bidlens/templates/my_shortlist.html").read_text()
        base = Path("src/bidlens/templates/base.html").read_text()
        card = Path("src/bidlens/templates/_opp_card.html").read_text()
        styles = Path("src/bidlens/static/css/styles.css").read_text()
        for template_name in ("feed.html", "triage.html", "archive.html"):
            self.assertNotIn(
                "shortlist-activity-rail",
                Path(f"src/bidlens/templates/{template_name}").read_text(),
            )
        self.assertIn("data-shortlist-activity-rail", shortlist)
        self.assertIn('class="side-card shortlist-activity-rail"', shortlist)
        self.assertIn('class="side-card-title">Recent Activity</div>', shortlist)
        self.assertIn('class="shortlist-companion-stack shortlist-companion-sticky"', shortlist)
        self.assertIn("mouseenter", shortlist)
        self.assertIn("focusin", shortlist)
        self.assertIn("selectShortlistPreview(shortlistPreviewCards[0])", shortlist)
        self.assertIn("aria-current", shortlist)
        self.assertIn("view == 'my_shortlist'", card)
        self.assertIn("active_page in ['feed', 'my_shortlist']", base)
        self.assertIn("work-sidebar--shortlist", base)
        self.assertIn("{% block work_sidebar_content %}", base)
        self.assertIn("{% block work_sidebar_content %}", shortlist)
        self.assertIn("data-work-sidebar-toggle", base)
        self.assertIn("bidlens.workSidebarCollapsed", base)
        self.assertIn("work-sidebar-is-collapsed .layout[data-work-layout]", styles)
        self.assertIn(".shortlist-activity-rail", styles)
        self.assertIn("min-height: 560px", styles)
        self.assertRegex(
            styles,
            r"@media \(max-width: 960px\)[\s\S]*?\.shortlist-activity-rail\s*\{\s*min-height: 0;",
        )
        self.assertNotIn("shortlist-content-layout", shortlist)
        self.assertNotIn(".shortlist-content-layout", styles)

    def test_feed_default_companion_content_and_shortlist_calendar_remain(self):
        base = Path("src/bidlens/templates/base.html").read_text()
        shortlist = Path("src/bidlens/templates/my_shortlist.html").read_text()
        styles = Path("src/bidlens/static/css/styles.css").read_text()

        self.assertIn('class="side-card-title"><a href="/my-shortlist', base)
        self.assertIn("calendar_drawer(calendar_items", shortlist)
        self.assertIn("data-calendar-drawer-toggle", Path("src/bidlens/templates/_calendar_drawer.html").read_text())
        self.assertNotIn("positionShortlistCalendarTab", shortlist)
        self.assertRegex(
            shortlist,
            r"shortlist-companion-stack[\s\S]*shortlist-activity-rail[\s\S]*calendar_drawer",
        )
        self.assertRegex(
            styles,
            r"\.shortlist-companion-sticky\s*\{[\s\S]*?position: sticky;[\s\S]*?top: 96px;",
        )
        self.assertRegex(
            styles,
            r"\.work-sidebar--shortlist\s*\{[\s\S]*?align-self: stretch;",
        )
        self.assertRegex(
            styles,
            r"\.work-sidebar--shortlist \.work-sidebar-content\s*\{[\s\S]*?flex: 1 1 auto;",
        )
        self.assertIn("max-height: calc(100vh - 112px)", styles)
        self.assertIn("writing-mode: horizontal-tb", styles)
        self.assertNotIn("rotate(270deg)", styles)


if __name__ == "__main__":
    unittest.main()
