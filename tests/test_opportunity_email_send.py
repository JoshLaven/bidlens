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
    MICROSOFT_SEND_MAIL_URL,
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

    def test_microsoft_scopes_include_mail_send_but_not_mail_read(self):
        scopes = {scope.lower() for scope in MICROSOFT_SCOPES}

        self.assertIn("mail.send", scopes)
        self.assertNotIn("mail.read", scopes)
        self.assertNotIn("mail.readbasic", scopes)
        self.assertNotIn("mail.readwrite", scopes)

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

    def test_message_validation_and_footer_do_not_expose_internal_id(self):
        self.assertEqual(validate_subject("  Hello   there  "), "Hello there")
        self.assertEqual(validate_message("Line\r\nTwo"), "Line\nTwo")
        body = body_with_footer(message="<b>Hello</b>", opportunity=self.opportunity)

        self.assertIn("<b>Hello</b>", body)
        self.assertIn("Sent from BidLens regarding Behavioral Health Services", body)
        self.assertNotIn(str(self.opportunity.id), body)

    @patch("bidlens.services.microsoft.requests.post")
    @patch("bidlens.services.microsoft.requests.get")
    def test_send_mail_uses_me_sendmail_and_202_maps_to_accepted(self, mock_get, mock_post):
        mock_get.return_value = _response(payload={"id": "ms-user-1", "userPrincipalName": "sender@microsoft.example"})
        mock_post.return_value = _response(ok=True, status_code=202)

        outcome = MicrosoftConnectionService(
            db=self.db,
            workspace=self.workspace,
            user=self.user,
        ).send_mail_for_current_user(
            to_recipients=["recipient@example.com"],
            subject="Discussion",
            body_text="Plain text",
        )

        self.assertEqual(outcome, {"status": "accepted_for_delivery"})
        self.assertEqual(mock_post.call_args.args[0], MICROSOFT_SEND_MAIL_URL)
        self.assertNotIn("/users/", mock_post.call_args.args[0])

    @patch("bidlens.services.microsoft.requests.post")
    @patch("bidlens.services.microsoft.requests.get")
    def test_timeout_maps_to_outcome_uncertain(self, mock_get, mock_post):
        import requests
        mock_get.return_value = _response(payload={"id": "ms-user-1", "userPrincipalName": "sender@microsoft.example"})
        mock_post.side_effect = requests.Timeout()

        with self.assertRaises(MicrosoftConnectionError) as context:
            MicrosoftConnectionService(db=self.db, workspace=self.workspace, user=self.user).send_mail_for_current_user(
                to_recipients=["recipient@example.com"],
                subject="Discussion",
                body_text="Plain text",
            )

        self.assertEqual(context.exception.code, "outcome_uncertain")

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
