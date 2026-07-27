import datetime as dt
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import (
    ExternalIntegrationConnection,
    Opportunity,
    OpportunityCommunicationMessage,
    OpportunityConversation,
    Organization,
    User,
    Workspace,
)
from bidlens.services.integration_credentials import encrypt_credentials
from bidlens.services.microsoft import MicrosoftConnectionError, MicrosoftConnectionService
from bidlens.services.microsoft_conversation_sync import sync_tracked_microsoft_conversations
from bidlens.services.opportunity_conversations import get_opportunity_conversation_context, safe_message_body


def graph_message(identifier, conversation="thread-1", sender="sender@example.com", *, draft=False):
    return {
        "id": identifier,
        "conversationId": conversation,
        "internetMessageId": f"<{identifier}@example.com>",
        "sender": {"emailAddress": {"address": sender, "name": "Sender"}},
        "toRecipients": [{"emailAddress": {"address": "to@example.com", "name": "Recipient"}}],
        "ccRecipients": [],
        "subject": "Tracked subject",
        "body": {"contentType": "html", "content": "<p>Hello <strong>there</strong></p>"},
        "sentDateTime": "2026-07-25T12:00:00Z",
        "webLink": "https://outlook.office.com/mail/item/example",
        "isDraft": draft,
    }


class MicrosoftConversationSyncTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Sync Org", slug="sync-org")
        self.db.add(self.org)
        self.db.flush()
        self.workspace = Workspace(organization_id=self.org.id, name="Sync", slug="sync")
        self.user = User(email="owner@example.com", name="Owner", organization_id=self.org.id)
        self.db.add_all([self.workspace, self.user])
        self.db.flush()
        self.opportunity = Opportunity(
            organization_id=self.org.id, source="sam", source_record_id="sync-opp", title="Sync Opportunity",
            agency="Agency", opportunity_type="Solicitation", description="Description",
            posted_date=dt.date(2026, 7, 1), response_deadline=dt.date(2026, 8, 1),
        )
        self.db.add(self.opportunity)
        self.db.flush()
        self.connection = ExternalIntegrationConnection(
            workspace_id=self.workspace.id, user_id=self.user.id, provider="microsoft",
            connection_status="connected", external_user_id="mailbox-1", connected_email="owner@example.com",
            granted_scopes="Mail.Send Mail.ReadWrite",
            encrypted_access_token=encrypt_credentials({"token": "token"}),
        )
        self.conversation = OpportunityConversation(
            workspace_id=self.workspace.id, opportunity_id=self.opportunity.id, provider="microsoft",
            external_conversation_id="thread-1", subject="Tracked subject", started_by_user_id=self.user.id,
            provider_mailbox_id="mailbox-1", initial_provider_message_id="initial-1", tracking_status="tracked",
        )
        self.db.add_all([self.connection, self.conversation])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @patch.object(MicrosoftConnectionService, "list_conversation_messages")
    def test_imports_inbound_and_outbound_messages_and_is_idempotent(self, list_messages):
        list_messages.return_value = [
            graph_message("out-1", sender="OWNER@example.com"),
            graph_message("in-1", sender="vendor@example.com"),
        ]
        first = sync_tracked_microsoft_conversations(self.db, workspace=self.workspace)
        second = sync_tracked_microsoft_conversations(self.db, workspace=self.workspace)

        self.assertEqual(first["new_messages_imported"], 2)
        self.assertEqual(second["new_messages_imported"], 0)
        self.assertEqual(second["duplicates_skipped"], 2)
        rows = self.db.query(OpportunityCommunicationMessage).order_by(OpportunityCommunicationMessage.provider_message_id).all()
        self.assertEqual({row.direction for row in rows}, {"inbound", "outbound"})
        self.assertEqual(self.db.get(OpportunityConversation, self.conversation.id).message_count, 2)

    @patch.object(MicrosoftConnectionService, "list_conversation_messages")
    def test_skips_drafts_invalid_conversation_and_untracked_rows(self, list_messages):
        self.db.add(OpportunityConversation(
            workspace_id=self.workspace.id, opportunity_id=self.opportunity.id, provider="microsoft",
            external_conversation_id="not-originated", subject="Not eligible", tracking_status="tracked",
        ))
        self.db.commit()
        list_messages.return_value = [
            graph_message("draft", draft=True),
            graph_message("wrong", conversation="another-thread"),
            graph_message("valid"),
        ]

        result = sync_tracked_microsoft_conversations(self.db, workspace=self.workspace)

        self.assertEqual(result["conversations_checked"], 1)
        self.assertEqual(result["messages_skipped"], 2)
        self.assertEqual(result["new_messages_imported"], 1)
        self.assertEqual(self.db.get(OpportunityConversation, self.conversation.id).tracking_status, "tracking_error")

    @patch.object(MicrosoftConnectionService, "list_conversation_messages")
    def test_conversation_failure_isolated_and_sanitized(self, list_messages):
        second = OpportunityConversation(
            workspace_id=self.workspace.id, opportunity_id=self.opportunity.id, provider="microsoft",
            external_conversation_id="thread-2", subject="Second", started_by_user_id=self.user.id,
            provider_mailbox_id="mailbox-1", initial_provider_message_id="initial-2", tracking_status="tracked",
        )
        self.db.add(second)
        self.db.commit()
        list_messages.side_effect = [RuntimeError("secret provider payload"), [graph_message("ok", conversation="thread-2")]]

        result = sync_tracked_microsoft_conversations(self.db, workspace=self.workspace)

        self.assertEqual(result["conversations_failed"], 1)
        self.assertEqual(result["new_messages_imported"], 1)
        self.assertEqual(self.db.get(OpportunityConversation, self.conversation.id).last_sync_error, "sync_failed")
        self.assertNotIn("secret", self.db.get(OpportunityConversation, self.conversation.id).last_sync_error)

    @patch.object(MicrosoftConnectionService, "list_conversation_messages")
    def test_imported_message_is_available_to_safe_communication_timeline(self, list_messages):
        message = graph_message("safe-body")
        message["body"]["content"] = "<p>Useful reply</p><script>alert('no')</script>"
        list_messages.return_value = [message]

        sync_tracked_microsoft_conversations(self.db, workspace=self.workspace)
        context = get_opportunity_conversation_context(self.db, opportunity=self.opportunity)
        timeline_message = context["conversations"][0]["messages"][0]

        self.assertEqual(timeline_message["body"], "Useful reply")
        self.assertNotIn("provider_message", str(timeline_message))
        self.assertNotIn("webLink", str(timeline_message))

    def test_plain_and_html_body_normalization(self):
        self.assertEqual(safe_message_body("plain <tag>", "text"), "plain <tag>")
        self.assertEqual(safe_message_body("<div>Hello<br>world</div><style>secret</style>", "html"), "Hello\nworld")

    @patch.object(MicrosoftConnectionService, "list_conversation_messages")
    def test_sent_items_copy_with_same_internet_message_id_is_not_reimported(self, list_messages):
        existing = OpportunityCommunicationMessage(
            workspace_id=self.workspace.id,
            opportunity_id=self.opportunity.id,
            conversation_id=self.conversation.id,
            associated_user_id=self.user.id,
            provider="microsoft",
            direction="outbound",
            provider_mailbox_id="mailbox-1",
            provider_message_id="send-endpoint-id",
            provider_conversation_id="thread-1",
            internet_message_id="<same-message@example.com>",
            sender_address="owner@example.com",
            recipients_json=[{"address": "to@example.com", "name": "Recipient"}],
            subject="Tracked subject",
        )
        self.db.add(existing)
        self.db.commit()
        sent_copy = graph_message("sent-items-id", sender="owner@example.com")
        sent_copy["internetMessageId"] = "<same-message@example.com>"
        list_messages.return_value = [sent_copy]

        result = sync_tracked_microsoft_conversations(self.db, workspace=self.workspace)

        self.assertEqual(result["new_messages_imported"], 0)
        self.assertEqual(result["duplicates_skipped"], 1)
        self.assertEqual(self.db.query(OpportunityCommunicationMessage).count(), 1)

    @patch.object(MicrosoftConnectionService, "list_conversation_messages")
    def test_scheduled_mode_stops_after_authorization_failure_and_marks_connection(self, list_messages):
        self.db.add(OpportunityConversation(
            workspace_id=self.workspace.id, opportunity_id=self.opportunity.id, provider="microsoft",
            external_conversation_id="thread-2", subject="Second", started_by_user_id=self.user.id,
            provider_mailbox_id="mailbox-1", initial_provider_message_id="initial-2", tracking_status="tracked",
        ))
        self.db.commit()
        list_messages.side_effect = MicrosoftConnectionError("reauthorization_required", "token=do-not-log")

        result = sync_tracked_microsoft_conversations(
            self.db, workspace=self.workspace, stop_on_authorization_failure=True,
        )

        self.assertEqual(result["conversations_checked"], 1)
        self.assertEqual(result["reauthorization_required"], 1)
        self.assertEqual(list_messages.call_count, 1)
        self.db.refresh(self.connection)
        self.assertEqual(self.connection.connection_status, "reauthorization_required")
        self.assertNotIn("do-not-log", self.connection.last_error_message)


class MicrosoftConversationGraphTests(unittest.TestCase):
    @patch("bidlens.services.microsoft.requests.get")
    def test_conversation_filter_immutable_ids_and_next_link_pagination(self, get):
        db = Mock()
        workspace = Mock(id=1)
        user = Mock(id=2)
        connection = Mock(
            workspace_id=1, user_id=2, provider="microsoft", connection_status="connected",
            granted_scopes="Mail.ReadWrite", encrypted_access_token="encrypted",
            access_token_expires_at=None,
        )
        service = MicrosoftConnectionService(db=db, workspace=workspace, user=user)
        service.connection = Mock(return_value=connection)
        service.access_token_for_connection = Mock(return_value="token")
        first = Mock(status_code=200)
        first.json.return_value = {"value": [graph_message("one")], "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=abc"}
        second = Mock(status_code=200)
        second.json.return_value = {"value": [graph_message("two")]}
        get.side_effect = [first, second]

        rows = service.list_conversation_messages("thread-1")

        self.assertEqual(len(rows), 2)
        first_call = get.call_args_list[0]
        self.assertEqual(first_call.kwargs["params"]["$filter"], "conversationId eq 'thread-1'")
        self.assertEqual(first_call.kwargs["headers"]["Prefer"], 'IdType="ImmutableId"')
        self.assertIsNone(get.call_args_list[1].kwargs["params"])


if __name__ == "__main__":
    unittest.main()
