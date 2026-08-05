import datetime as dt
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import (
    ExternalIntegrationConnection,
    Opportunity,
    OpportunityActivityEvent,
    OpportunityCommunicationMessage,
    OpportunityConversation,
    OpportunityConversationSendAttempt,
    Organization,
    OrganizationMembership,
    User,
    Vote,
    Workspace,
)
from bidlens.services.integration_credentials import encrypt_credentials
from bidlens.services.microsoft import (
    MICROSOFT_SCOPES,
    MICROSOFT_MESSAGES_URL,
    PROVIDER_MICROSOFT,
    STATUS_CONNECTED,
    MicrosoftConnectionError,
    MicrosoftConnectionService,
    connection_has_scope,
)
from bidlens.services.opportunity_email import (
    SEND_STATUS_ACCEPTED,
    OpportunityEmailValidationError,
    body_with_footer,
    ensure_user_shortlisted_from_email,
    finalize_accepted_send,
    interested_colleague_recipients,
    merge_recipients,
    parse_recipient_emails,
    reserve_send_attempt,
    send_token_digest,
    validate_message,
    validate_send_attempt,
    validate_subject,
)


def _response(ok=True, status_code=200, payload=None):
    response = Mock()
    response.ok = ok
    response.status_code = status_code
    response.json.return_value = payload or {}
    return response


class OpportunityEmailSendTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Email Org", slug="email-org")
        self.other_org = Organization(name="Other Email Org", slug="other-email-org")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.workspace = Workspace(organization_id=self.org.id, name="Email Workspace", slug="email-workspace")
        self.other_workspace = Workspace(organization_id=self.other_org.id, name="Other Workspace", slug="other-email-workspace")
        self.user = User(email="sender@example.com", name="Sender", organization_id=self.org.id)
        self.colleague = User(email="colleague@example.com", name="Colleague", organization_id=self.org.id)
        self.other_user = User(email="other@example.com", name="Other", organization_id=self.other_org.id)
        self.db.add_all([self.workspace, self.other_workspace, self.user, self.colleague, self.other_user])
        self.db.flush()
        self.db.add_all([
            OrganizationMembership(organization_id=self.org.id, user_id=self.user.id, role="member"),
            OrganizationMembership(organization_id=self.org.id, user_id=self.colleague.id, role="member"),
            OrganizationMembership(organization_id=self.other_org.id, user_id=self.other_user.id, role="member"),
        ])
        self.opportunity = Opportunity(
            organization_id=self.org.id,
            source="sam",
            source_record_id="email-opp",
            title="Behavioral Health Services",
            agency="HHS",
            opportunity_type="Solicitation",
            posted_date=dt.date(2026, 7, 1),
            response_deadline=dt.date(2026, 8, 1),
            qualification_status="qualified",
        )
        self.other_opportunity = Opportunity(
            organization_id=self.other_org.id,
            source="sam",
            source_record_id="other-email-opp",
            title="Other Opportunity",
            agency="NASA",
            opportunity_type="Solicitation",
            posted_date=dt.date(2026, 7, 1),
            response_deadline=dt.date(2026, 8, 1),
            qualification_status="qualified",
        )
        self.db.add_all([self.opportunity, self.other_opportunity])
        self.db.flush()
        self.db.add_all([
            Vote(org_id=self.org.id, opp_id=self.opportunity.id, user_id=self.user.id, vote="PURSUE"),
            Vote(org_id=self.org.id, opp_id=self.opportunity.id, user_id=self.colleague.id, vote="PURSUE"),
            Vote(org_id=self.other_org.id, opp_id=self.other_opportunity.id, user_id=self.other_user.id, vote="PURSUE"),
        ])
        self.connection = ExternalIntegrationConnection(
            workspace_id=self.workspace.id,
            user_id=self.user.id,
            provider=PROVIDER_MICROSOFT,
            connection_status=STATUS_CONNECTED,
            external_tenant_id="tenant-1",
            external_user_id="ms-user-1",
            connected_email="sender@microsoft.example",
            encrypted_access_token=encrypt_credentials({"token": "access"}),
            encrypted_refresh_token=encrypt_credentials({"token": "refresh"}),
            access_token_expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1),
            granted_scopes=" ".join(MICROSOFT_SCOPES),
        )
        self.db.add(self.connection)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def _provider_result(*, tracking_error=None):
        return {
            "status": "accepted_for_delivery",
            "provider": "microsoft",
            "provider_mailbox_id": "ms-user-1",
            "tracking_error": tracking_error,
            "message": {
                "id": "immutable-message-1",
                "conversationId": "outlook-conversation-1",
                "internetMessageId": "<internet-message-1@example.test>",
                "sender": {"emailAddress": {"address": "sender@microsoft.example", "name": "Sender"}},
                "toRecipients": [{"emailAddress": {"address": "recipient@example.com", "name": "Recipient"}}],
                "ccRecipients": [],
                "subject": "Discussion",
                "body": {"contentType": "text", "content": "Tracked body"},
                "sentDateTime": "2026-07-26T12:30:00Z",
                "webLink": "https://outlook.office.com/mail/deeplink/read/example",
            },
        }

    def test_microsoft_scopes_include_send_and_tracked_message_access(self):
        scopes = {scope.lower() for scope in MICROSOFT_SCOPES}

        self.assertIn("mail.send", scopes)
        self.assertIn("mail.readwrite", scopes)
        self.assertNotIn("mail.read", scopes)
        self.assertNotIn("mail.readbasic", scopes)

    def test_identity_only_connection_lacks_mail_send(self):
        self.connection.granted_scopes = "openid profile email offline_access User.Read"

        self.assertFalse(connection_has_scope(self.connection, "Mail.Send"))

    def test_recipient_validation_dedupes_and_rejects_injection(self):
        self.assertEqual(
            parse_recipient_emails("A@example.com, a@example.com; b@example.com"),
            ["A@example.com", "b@example.com"],
        )
        with self.assertRaises(OpportunityEmailValidationError):
            parse_recipient_emails("victim@example.com\nbcc:evil@example.com")
        with self.assertRaises(OpportunityEmailValidationError):
            parse_recipient_emails("not-an-email")

    def test_interested_colleagues_are_same_org_and_exclude_initiator(self):
        recipients = interested_colleague_recipients(
            self.db,
            opportunity=self.opportunity,
            current_user_id=self.user.id,
        )

        self.assertEqual([recipient.email for recipient in recipients], ["colleague@example.com"])

    def test_email_shortlist_signal_leaves_existing_pursue_unchanged(self):
        added = ensure_user_shortlisted_from_email(self.db, opportunity=self.opportunity, user=self.user)
        self.db.commit()

        vote = (
            self.db.query(Vote)
            .filter_by(org_id=self.org.id, opp_id=self.opportunity.id, user_id=self.user.id)
            .one()
        )
        self.assertFalse(added)
        self.assertEqual(vote.vote, "PURSUE")

    def test_email_shortlist_signal_sets_pursue_when_not_already_shortlisted(self):
        vote = (
            self.db.query(Vote)
            .filter_by(org_id=self.org.id, opp_id=self.opportunity.id, user_id=self.user.id)
            .one()
        )
        vote.vote = "PASS"
        self.db.commit()

        added = ensure_user_shortlisted_from_email(self.db, opportunity=self.opportunity, user=self.user)
        self.db.commit()

        self.assertTrue(added)
        self.assertEqual(vote.vote, "PURSUE")
        self.assertIsNotNone(vote.shortlisted_at)

    def test_message_validation_and_footer_do_not_expose_internal_id(self):
        self.assertEqual(validate_subject("  Hello   there  "), "Hello there")
        self.assertEqual(validate_message("Line\r\nTwo"), "Line\nTwo")
        body = body_with_footer(message="<b>Hello</b>", opportunity=self.opportunity)

        self.assertIn("<b>Hello</b>", body)
        self.assertIn("Sent from BidLens regarding Behavioral Health Services", body)
        self.assertNotIn(str(self.opportunity.id), body)

    @patch("bidlens.services.microsoft.requests.post")
    @patch("bidlens.services.microsoft.requests.get")
    def test_send_mail_uses_immutable_draft_and_persists_tracking_response(self, mock_get, mock_post):
        mock_get.side_effect = [
            _response(payload={"id": "ms-user-1", "userPrincipalName": "sender@microsoft.example"}),
            _response(payload={"id": "immutable-1", "conversationId": "conversation-1"}),
        ]
        mock_post.side_effect = [
            _response(ok=True, status_code=201, payload={"id": "immutable-1", "conversationId": "conversation-1"}),
            _response(ok=True, status_code=202),
        ]

        outcome = MicrosoftConnectionService(
            db=self.db,
            workspace=self.workspace,
            user=self.user,
        ).send_mail_for_current_user(
            to_recipients=["recipient@example.com"],
            subject="Discussion",
            body_text="Plain text",
        )

        self.assertEqual(outcome["status"], "accepted_for_delivery")
        self.assertEqual(outcome["message"]["id"], "immutable-1")
        self.assertEqual(outcome["message"]["conversationId"], "conversation-1")
        self.assertEqual(mock_post.call_args_list[0].args[0], MICROSOFT_MESSAGES_URL)
        self.assertEqual(mock_post.call_args_list[1].args[0], f"{MICROSOFT_MESSAGES_URL}/immutable-1/send")
        self.assertEqual(mock_post.call_args_list[0].kwargs["headers"]["Prefer"], 'IdType="ImmutableId"')

    @patch("bidlens.services.microsoft.requests.post")
    @patch("bidlens.services.microsoft.requests.get")
    def test_timeout_maps_to_outcome_uncertain(self, mock_get, mock_post):
        import requests
        mock_get.return_value = _response(payload={"id": "ms-user-1", "userPrincipalName": "sender@microsoft.example"})
        mock_post.side_effect = [
            _response(ok=True, status_code=201, payload={"id": "immutable-timeout"}),
            requests.Timeout(),
        ]

        with self.assertRaises(MicrosoftConnectionError) as context:
            MicrosoftConnectionService(db=self.db, workspace=self.workspace, user=self.user).send_mail_for_current_user(
                to_recipients=["recipient@example.com"],
                subject="Discussion",
                body_text="Plain text",
            )

        self.assertEqual(context.exception.code, "outcome_uncertain")

    @patch("bidlens.services.microsoft.time.sleep")
    @patch("bidlens.services.microsoft.requests.post")
    @patch("bidlens.services.microsoft.requests.get")
    def test_sent_message_retrieval_failure_returns_accepted_with_diagnostic(
        self,
        mock_get,
        mock_post,
        _sleep,
    ):
        mock_get.side_effect = [
            _response(payload={"id": "ms-user-1", "userPrincipalName": "sender@microsoft.example"}),
            _response(ok=False, status_code=404),
            _response(ok=False, status_code=404),
            _response(ok=False, status_code=404),
        ]
        mock_post.side_effect = [
            _response(ok=True, status_code=201, payload={
                "id": "immutable-retrieval-failure",
                "conversationId": "conversation-retrieval-failure",
            }),
            _response(ok=True, status_code=202),
        ]

        result = MicrosoftConnectionService(
            db=self.db,
            workspace=self.workspace,
            user=self.user,
        ).send_mail_for_current_user(
            to_recipients=["recipient@example.com"],
            subject="Discussion",
            body_text="Plain text",
        )

        self.assertEqual(result["status"], "accepted_for_delivery")
        self.assertEqual(result["tracking_error"], "metadata_retrieval_failed")
        self.assertEqual(result["message"]["id"], "immutable-retrieval-failure")
        self.assertEqual(result["message"]["conversationId"], "conversation-retrieval-failure")

    def test_idempotency_reservation_is_bound_to_user_and_opportunity(self):
        token = "server-generated-token"
        attempt = reserve_send_attempt(self.db, opportunity=self.opportunity, user=self.user, token=token)
        self.db.commit()

        replay = reserve_send_attempt(self.db, opportunity=self.opportunity, user=self.user, token=token)
        self.assertEqual(replay.id, attempt.id)
        self.assertEqual(attempt.idempotency_key_digest, send_token_digest(token))

        with self.assertRaises(OpportunityEmailValidationError):
            validate_send_attempt(attempt, opportunity=self.other_opportunity, user=self.user)

    def test_finalize_accepted_send_creates_conversation_and_activity_without_body(self):
        token = "accepted-token"
        attempt = reserve_send_attempt(self.db, opportunity=self.opportunity, user=self.user, token=token)
        self.db.commit()

        conversation = finalize_accepted_send(
            self.db,
            opportunity=self.opportunity,
            user=self.user,
            attempt=attempt,
            subject="Discussion",
            recipients=["recipient@example.com"],
        )
        self.db.commit()

        self.assertEqual(conversation.provider, "microsoft")
        self.assertIsNone(conversation.external_conversation_id)
        self.assertEqual(conversation.send_status, SEND_STATUS_ACCEPTED)
        self.assertEqual(conversation.message_count, 1)
        event = self.db.query(OpportunityActivityEvent).one()
        self.assertEqual(event.conversation_id, conversation.id)
        self.assertNotIn("Plain text", str(event.metadata_json or {}))
        self.assertEqual(self.db.query(OpportunityConversation).count(), 1)

    def test_outbound_tracking_metadata_is_persisted_at_message_and_conversation_levels(self):
        attempt = reserve_send_attempt(
            self.db,
            opportunity=self.opportunity,
            user=self.user,
            token="tracked-token",
        )
        conversation = finalize_accepted_send(
            self.db,
            opportunity=self.opportunity,
            user=self.user,
            attempt=attempt,
            subject="Discussion",
            recipients=["recipient@example.com"],
            body="Tracked body",
            provider_result=self._provider_result(),
        )
        self.db.commit()

        message = self.db.query(OpportunityCommunicationMessage).one()
        self.assertEqual(message.workspace_id, self.workspace.id)
        self.assertEqual(message.opportunity_id, self.opportunity.id)
        self.assertEqual(message.associated_user_id, self.user.id)
        self.assertEqual(message.provider, "microsoft")
        self.assertEqual(message.direction, "outbound")
        self.assertEqual(message.provider_message_id, "immutable-message-1")
        self.assertEqual(message.provider_conversation_id, "outlook-conversation-1")
        self.assertNotEqual(message.provider_message_id, message.provider_conversation_id)
        self.assertEqual(message.internet_message_id, "<internet-message-1@example.test>")
        self.assertEqual(message.recipients_json[0]["address"], "recipient@example.com")
        self.assertEqual(message.subject, "Discussion")
        self.assertEqual(conversation.external_conversation_id, "outlook-conversation-1")
        self.assertEqual(conversation.initial_provider_message_id, "immutable-message-1")
        self.assertEqual(conversation.tracking_status, "tracked")

    def test_tracking_metadata_failure_preserves_outbound_record_and_shortlist(self):
        vote = self.db.query(Vote).filter_by(
            org_id=self.org.id,
            opp_id=self.opportunity.id,
            user_id=self.user.id,
        ).one()
        vote.vote = "PASS"
        attempt = reserve_send_attempt(
            self.db,
            opportunity=self.opportunity,
            user=self.user,
            token="tracking-failure-token",
        )
        conversation = finalize_accepted_send(
            self.db,
            opportunity=self.opportunity,
            user=self.user,
            attempt=attempt,
            subject="Discussion",
            recipients=["recipient@example.com"],
            body="Tracked body",
            provider_result=self._provider_result(tracking_error="metadata_retrieval_failed"),
        )
        ensure_user_shortlisted_from_email(self.db, opportunity=self.opportunity, user=self.user)
        self.db.commit()

        self.assertEqual(conversation.tracking_status, "tracking_error")
        self.assertEqual(conversation.last_sync_error, "metadata_retrieval_failed")
        self.assertEqual(self.db.query(OpportunityCommunicationMessage).count(), 1)
        self.assertEqual(vote.vote, "PURSUE")

    def test_duplicate_provider_message_id_is_idempotent_within_mailbox(self):
        first_attempt = reserve_send_attempt(
            self.db,
            opportunity=self.opportunity,
            user=self.user,
            token="duplicate-one",
        )
        first = finalize_accepted_send(
            self.db,
            opportunity=self.opportunity,
            user=self.user,
            attempt=first_attempt,
            subject="Discussion",
            recipients=["recipient@example.com"],
            provider_result=self._provider_result(),
        )
        self.db.commit()
        second_attempt = reserve_send_attempt(
            self.db,
            opportunity=self.opportunity,
            user=self.user,
            token="duplicate-two",
        )
        second = finalize_accepted_send(
            self.db,
            opportunity=self.opportunity,
            user=self.user,
            attempt=second_attempt,
            subject="Discussion",
            recipients=["recipient@example.com"],
            provider_result=self._provider_result(),
        )
        self.db.commit()

        self.assertEqual(first.id, second.id)
        self.assertEqual(self.db.query(OpportunityCommunicationMessage).count(), 1)

    def test_salesforce_failure_after_email_keeps_conversation_and_shortlist(self):
        from bidlens.routes import opportunities

        vote = self.db.query(Vote).filter_by(
            org_id=self.org.id,
            opp_id=self.opportunity.id,
            user_id=self.user.id,
        ).one()
        vote.vote = "PASS"
        attempt = reserve_send_attempt(
            self.db,
            opportunity=self.opportunity,
            user=self.user,
            token="salesforce-failure-token",
        )
        finalize_accepted_send(
            self.db,
            opportunity=self.opportunity,
            user=self.user,
            attempt=attempt,
            subject="Discussion",
            recipients=["recipient@example.com"],
            provider_result=self._provider_result(),
        )
        self.assertTrue(
            ensure_user_shortlisted_from_email(
                self.db,
                opportunity=self.opportunity,
                user=self.user,
            )
        )
        self.db.commit()

        service = Mock()
        service.is_authorized.return_value = True
        with (
            patch.object(opportunities, "SalesforceService", return_value=service),
            patch.object(opportunities, "ensure_opportunity_in_salesforce", side_effect=RuntimeError("CRM unavailable")),
            patch.object(opportunities, "record_salesforce_sync_failure") as record_failure,
        ):
            opportunities._sync_emailed_opportunity_to_salesforce(
                self.db,
                opportunity=self.opportunity,
                user=self.user,
            )

        persisted_vote = self.db.query(Vote).filter_by(
            org_id=self.org.id,
            opp_id=self.opportunity.id,
            user_id=self.user.id,
        ).one()
        self.assertEqual(persisted_vote.vote, "PURSUE")
        self.assertEqual(self.db.query(OpportunityConversation).count(), 1)
        tracked = self.db.query(OpportunityCommunicationMessage).one()
        self.assertEqual(tracked.provider_message_id, "immutable-message-1")
        record_failure.assert_called_once()

    def test_email_salesforce_sync_runs_only_when_connected(self):
        from bidlens.routes import opportunities

        service = Mock()
        service.is_authorized.return_value = False
        with (
            patch.object(opportunities, "SalesforceService", return_value=service),
            patch.object(opportunities, "ensure_opportunity_in_salesforce") as ensure_salesforce,
        ):
            opportunities._sync_emailed_opportunity_to_salesforce(
                self.db,
                opportunity=self.opportunity,
                user=self.user,
            )
        ensure_salesforce.assert_not_called()

        service.is_authorized.return_value = True
        with (
            patch.object(opportunities, "SalesforceService", return_value=service),
            patch.object(opportunities, "ensure_opportunity_in_salesforce") as ensure_salesforce,
        ):
            opportunities._sync_emailed_opportunity_to_salesforce(
                self.db,
                opportunity=self.opportunity,
                user=self.user,
            )
        ensure_salesforce.assert_called_once()


if __name__ == "__main__":
    unittest.main()
