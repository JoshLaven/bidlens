import datetime as dt
import os
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from jinja2 import Environment

from bidlens.database import Base
from bidlens.models import (
    Opportunity,
    OpportunityActivityEvent,
    OpportunityConversation,
    Organization,
    User,
    Workspace,
)
from bidlens.services.opportunity_conversations import (
    DEFAULT_RECENT_ACTIVITY_LIMIT,
    EVENT_TYPE_CONVERSATION_MESSAGE,
    EVENT_TYPE_CONVERSATION_STARTED,
    OpportunityConversationTenancyError,
    create_activity_for_authorized_opportunity,
    create_conversation_for_authorized_opportunity,
    display_safe_metadata,
    get_opportunity_conversation_context,
    human_activity_text,
    provider_display_name,
)
from scripts.seed_opportunity_conversations import assert_safe_local_seed_environment


class OpportunityConversationFoundationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Conversation Org", slug="conversation-org")
        self.other_org = Organization(name="Other Conversation Org", slug="other-conversation-org")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.workspace = Workspace(
            organization_id=self.org.id,
            name="Conversation Workspace",
            slug="conversation-workspace",
        )
        self.other_workspace = Workspace(
            organization_id=self.other_org.id,
            name="Other Conversation Workspace",
            slug="other-conversation-workspace",
        )
        self.db.add_all([self.workspace, self.other_workspace])
        self.db.flush()
        self.user = User(email="conversation@example.com", name="Casey Capture", organization_id=self.org.id)
        self.db.add(self.user)
        self.db.flush()
        self.opportunity = self._opportunity(self.org.id, "Opportunity with conversations")
        self.other_opportunity = self._opportunity(self.other_org.id, "Other workspace opportunity")
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
            source_record_id=f"src-{organization_id}-{title}",
            title=title,
            agency="Department of Health and Human Services",
            opportunity_type="Solicitation",
            posted_date=dt.date(2026, 7, 1),
            response_deadline=dt.date(2026, 8, 1),
            description="Test opportunity.",
            qualification_status="qualified",
        )

    def test_multiple_conversations_render_in_newest_first_context(self):
        now = dt.datetime(2026, 7, 23, 12, 0)
        older = OpportunityConversation(
            workspace_id=self.workspace.id,
            opportunity_id=self.opportunity.id,
            provider="manual",
            subject="Older capture thread",
            started_by_user_id=self.user.id,
            message_count=1,
            last_message_at=now - dt.timedelta(hours=2),
        )
        newer = OpportunityConversation(
            workspace_id=self.workspace.id,
            opportunity_id=self.opportunity.id,
            provider="manual",
            subject="Newer capture thread",
            started_by_user_id=self.user.id,
            message_count=2,
            last_message_at=now,
        )
        self.db.add_all([older, newer])
        self.db.flush()
        self.db.add_all([
            OpportunityActivityEvent(
                workspace_id=self.workspace.id,
                opportunity_id=self.opportunity.id,
                conversation_id=older.id,
                actor_user_id=self.user.id,
                event_type=EVENT_TYPE_CONVERSATION_STARTED,
                title="Older event",
                occurred_at=now - dt.timedelta(hours=2),
            ),
            OpportunityActivityEvent(
                workspace_id=self.workspace.id,
                opportunity_id=self.opportunity.id,
                conversation_id=newer.id,
                actor_user_id=self.user.id,
                event_type=EVENT_TYPE_CONVERSATION_MESSAGE,
                title="Newest event",
                occurred_at=now,
            ),
        ])
        self.db.commit()

        context = get_opportunity_conversation_context(
            self.db,
            opportunity=self.opportunity,
        )

        self.assertEqual([row["subject"] for row in context["conversations"]], ["Newer capture thread", "Older capture thread"])
        self.assertEqual([row["text"] for row in context["recent_activity"]], ["Newest event", "Older event"])
        self.assertIn("Automated status summaries", context["current_status"]["narrative"])
        self.assertEqual(
            context["current_status"]["summary_updated_label"],
            "Automated summary not yet generated.",
        )

    def test_context_is_workspace_scoped(self):
        event = OpportunityActivityEvent(
            workspace_id=self.other_workspace.id,
            opportunity_id=self.other_opportunity.id,
            event_type=EVENT_TYPE_CONVERSATION_STARTED,
            title="Other workspace event",
            occurred_at=dt.datetime(2026, 7, 23, 10, 0),
        )
        self.db.add(event)
        self.db.commit()

        context = get_opportunity_conversation_context(
            self.db,
            opportunity=self.opportunity,
        )

        self.assertEqual(context["conversations"], [])
        self.assertEqual(context["recent_activity"], [])
        self.assertEqual(
            context["current_status"]["narrative"],
            "No conversation activity has been recorded yet.",
        )

    def test_human_activity_text_has_readable_fallbacks(self):
        event = OpportunityActivityEvent(
            workspace_id=self.workspace.id,
            opportunity_id=self.opportunity.id,
            actor_user_id=self.user.id,
            actor=self.user,
            event_type=EVENT_TYPE_CONVERSATION_STARTED,
            title="",
        )

        self.assertEqual(human_activity_text(event), "Casey Capture started a conversation.")

    def test_creation_helpers_derive_workspace_from_authorized_opportunity(self):
        conversation = create_conversation_for_authorized_opportunity(
            self.db,
            opportunity=self.opportunity,
            subject="Derived workspace thread",
            started_by_user_id=self.user.id,
        )
        self.db.flush()
        event = create_activity_for_authorized_opportunity(
            self.db,
            opportunity=self.opportunity,
            conversation=conversation,
            actor_user_id=self.user.id,
            event_type=EVENT_TYPE_CONVERSATION_MESSAGE,
            title="Derived workspace event",
        )
        self.db.flush()

        self.assertEqual(conversation.workspace_id, self.workspace.id)
        self.assertEqual(conversation.opportunity_id, self.opportunity.id)
        self.assertEqual(event.workspace_id, self.workspace.id)
        self.assertEqual(event.opportunity_id, self.opportunity.id)

    def test_service_rejects_opportunity_without_workspace(self):
        orphan_org = Organization(name="Legacy Org", slug="legacy-org")
        self.db.add(orphan_org)
        self.db.flush()
        orphan_opportunity = self._opportunity(orphan_org.id, "Legacy opportunity")
        self.db.add(orphan_opportunity)
        self.db.commit()

        with self.assertRaises(OpportunityConversationTenancyError):
            get_opportunity_conversation_context(self.db, opportunity=orphan_opportunity)

    def test_activity_creation_rejects_conversation_from_another_opportunity(self):
        other_conversation = create_conversation_for_authorized_opportunity(
            self.db,
            opportunity=self.other_opportunity,
            subject="Other opportunity thread",
        )
        self.db.flush()

        with self.assertRaises(OpportunityConversationTenancyError):
            create_activity_for_authorized_opportunity(
                self.db,
                opportunity=self.opportunity,
                conversation=other_conversation,
                event_type=EVENT_TYPE_CONVERSATION_MESSAGE,
                title="Invalid cross-opportunity activity",
            )

    def test_retrieval_limit_is_newest_first_and_explicit(self):
        now = dt.datetime(2026, 7, 23, 12, 0)
        for index in range(DEFAULT_RECENT_ACTIVITY_LIMIT + 3):
            self.db.add(
                OpportunityActivityEvent(
                    workspace_id=self.workspace.id,
                    opportunity_id=self.opportunity.id,
                    event_type=EVENT_TYPE_CONVERSATION_MESSAGE,
                    title=f"Event {index}",
                    occurred_at=now + dt.timedelta(minutes=index),
                )
            )
        self.db.commit()

        context = get_opportunity_conversation_context(self.db, opportunity=self.opportunity)

        self.assertEqual(len(context["recent_activity"]), DEFAULT_RECENT_ACTIVITY_LIMIT)
        self.assertEqual(context["recent_activity"][0]["text"], f"Event {DEFAULT_RECENT_ACTIVITY_LIMIT + 2}")

    def test_duplicate_provider_external_id_rejected_in_same_workspace(self):
        self.db.add_all([
            OpportunityConversation(
                workspace_id=self.workspace.id,
                opportunity_id=self.opportunity.id,
                provider="microsoft_365",
                external_conversation_id="thread-123",
                subject="Thread A",
            ),
            OpportunityConversation(
                workspace_id=self.workspace.id,
                opportunity_id=self.opportunity.id,
                provider="microsoft_365",
                external_conversation_id="thread-123",
                subject="Thread B",
            ),
        ])

        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

    def test_null_external_ids_remain_allowed(self):
        self.db.add_all([
            OpportunityConversation(
                workspace_id=self.workspace.id,
                opportunity_id=self.opportunity.id,
                provider="manual",
                external_conversation_id=None,
                subject="Manual Thread A",
            ),
            OpportunityConversation(
                workspace_id=self.workspace.id,
                opportunity_id=self.opportunity.id,
                provider="manual",
                external_conversation_id=None,
                subject="Manual Thread B",
            ),
        ])

        self.db.commit()
        self.assertEqual(
            self.db.query(OpportunityConversation).filter(OpportunityConversation.external_conversation_id.is_(None)).count(),
            2,
        )

    def test_unknown_provider_and_metadata_have_safe_display_boundaries(self):
        self.assertEqual(provider_display_name("<script>alert(1)</script>"), "External Provider")
        metadata = display_safe_metadata(
            {
                "source": "<script>alert(1)</script>",
                "access_token": "secret-token",
                "raw_response": {"body": "<img src=x onerror=alert(1)>"},
            }
        )

        self.assertEqual(metadata, {"source": "<script>alert(1)</script>"})
        self.assertNotIn("access_token", metadata)
        self.assertNotIn("raw_response", metadata)

    def test_untrusted_values_are_escaped_when_rendered(self):
        template = Environment(autoescape=True).from_string(
            "{{ subject }} {{ participants }} {{ title }} {{ description }} {{ metadata.source }}"
        )
        rendered = template.render(
            subject="<script>alert(1)</script>",
            participants="<img src=x onerror=alert(1)>",
            title='<a href="javascript:alert(1)">Click</a>',
            description="<b>description</b>",
            metadata={"source": "<svg onload=alert(1)>"},
        )

        self.assertNotIn("<script>", rendered)
        self.assertNotIn("<img", rendered)
        self.assertNotIn("<a href", rendered)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)

    def test_detail_template_contains_foundation_sections_and_empty_states(self):
        with open("src/bidlens/templates/detail.html", encoding="utf-8") as template:
            html = template.read()

        self.assertIn("Current Status", html)
        self.assertIn("Conversations", html)
        self.assertIn("Recent Activity", html)
        self.assertIn("Automated summary not yet generated.", html)
        self.assertIn("No conversations have been associated with this opportunity yet.", html)
        self.assertIn("No recent activity has been recorded yet.", html)

    def test_deleting_opportunity_removes_conversations_and_activity(self):
        conversation = create_conversation_for_authorized_opportunity(
            self.db,
            opportunity=self.opportunity,
            subject="Delete with opportunity",
        )
        self.db.flush()
        create_activity_for_authorized_opportunity(
            self.db,
            opportunity=self.opportunity,
            conversation=conversation,
            event_type=EVENT_TYPE_CONVERSATION_MESSAGE,
            title="Delete with opportunity",
        )
        self.db.commit()

        self.db.delete(self.opportunity)
        self.db.commit()

        self.assertEqual(self.db.query(OpportunityConversation).count(), 0)
        self.assertEqual(self.db.query(OpportunityActivityEvent).count(), 0)

    def test_deleting_conversation_removes_conversation_specific_activity(self):
        conversation = create_conversation_for_authorized_opportunity(
            self.db,
            opportunity=self.opportunity,
            subject="Delete conversation",
        )
        self.db.flush()
        create_activity_for_authorized_opportunity(
            self.db,
            opportunity=self.opportunity,
            conversation=conversation,
            event_type=EVENT_TYPE_CONVERSATION_MESSAGE,
            title="Conversation event",
        )
        create_activity_for_authorized_opportunity(
            self.db,
            opportunity=self.opportunity,
            event_type=EVENT_TYPE_CONVERSATION_MESSAGE,
            title="Opportunity-level event",
        )
        self.db.commit()

        self.db.delete(conversation)
        self.db.commit()

        self.assertEqual(self.db.query(OpportunityConversation).count(), 0)
        self.assertEqual(self.db.query(OpportunityActivityEvent).count(), 1)

    def test_seed_utility_refuses_production_environment(self):
        with patch.dict(os.environ, {"BIDLENS_ENV": "production"}, clear=False):
            with self.assertRaises(SystemExit):
                assert_safe_local_seed_environment()


if __name__ == "__main__":
    unittest.main()
