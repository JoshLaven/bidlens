import datetime as dt
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import Opportunity, OpportunityCommunicationMessage, OpportunityConversation, Organization, User, Workspace
from bidlens.services.communication_summary import (
    CommunicationSummaryError,
    CommunicationSummaryResult,
    clean_message_body,
    csrf_token,
    generate_and_save_summary,
    prepare_communication_input,
    select_messages,
    summary_is_stale,
    validate_csrf_token,
)


class FakeGenerator:
    def __init__(self, fail=False): self.fail = fail
    def generate_summary(self, input_data):
        if self.fail:
            raise CommunicationSummaryError("timeout", "safe")
        return CommunicationSummaryResult("Awaiting reply", ["Proposal sent"], ["Is Friday acceptable?"], "Follow up Friday", "external_party", "test", "test-model", {}, {"openai_api_request": 10.0})


class CommunicationSummaryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Org", slug="summary-org")
        self.other_org = Organization(name="Other", slug="summary-other")
        self.db.add_all([self.org, self.other_org]); self.db.flush()
        self.workspace = Workspace(organization_id=self.org.id, name="Workspace", slug="summary-workspace")
        self.other_workspace = Workspace(organization_id=self.other_org.id, name="Other", slug="summary-other-workspace")
        self.user = User(email="member@example.com", organization_id=self.org.id)
        self.db.add_all([self.workspace, self.other_workspace, self.user]); self.db.flush()
        self.opp = self._opp(self.org.id, "one")
        self.other_opp = self._opp(self.org.id, "two")
        self.cross_opp = self._opp(self.other_org.id, "three")
        self.db.add_all([self.opp, self.other_opp, self.cross_opp]); self.db.flush()
        self.conv1 = self._conversation(self.workspace.id, self.opp.id, "Thread one")
        self.conv2 = self._conversation(self.workspace.id, self.opp.id, "Thread two")
        self.other_conv = self._conversation(self.workspace.id, self.other_opp.id, "Other opportunity")
        self.cross_conv = self._conversation(self.other_workspace.id, self.cross_opp.id, "Other workspace")
        self.db.add_all([self.conv1, self.conv2, self.other_conv, self.cross_conv]); self.db.flush()

    def tearDown(self):
        self.db.close(); self.engine.dispose()

    @staticmethod
    def _opp(org_id, key):
        return Opportunity(organization_id=org_id, source="sam", source_record_id=f"summary-{org_id}-{key}", title=key, agency="Agency", opportunity_type="Solicitation", posted_date=dt.date(2026, 7, 1), response_deadline=dt.date(2026, 8, 1), qualification_status="qualified")

    @staticmethod
    def _conversation(workspace_id, opportunity_id, subject):
        return OpportunityConversation(workspace_id=workspace_id, opportunity_id=opportunity_id, provider="microsoft", subject=subject)

    def _message(self, conv, minute, body="Substantive update", user_id=None):
        row = OpportunityCommunicationMessage(workspace_id=conv.workspace_id, opportunity_id=conv.opportunity_id, conversation_id=conv.id, associated_user_id=user_id or self.user.id, provider="microsoft", direction="inbound", provider_mailbox_id="secret-mailbox", provider_message_id=f"secret-{conv.id}-{minute}", provider_conversation_id="secret-thread", internet_message_id="secret-internet", sender_address="sender@example.com", recipients_json=[{"address": "team@example.com"}], subject="Update", body=body, body_content_type="text", provider_timestamp=dt.datetime(2026, 7, 27, 12, minute), provider_web_link="https://secret")
        self.db.add(row); self.db.flush(); return row

    def test_selection_scopes_orders_and_uses_multiple_conversations_and_users(self):
        second_user = User(email="other@example.com", organization_id=self.org.id); self.db.add(second_user); self.db.flush()
        newer = self._message(self.conv2, 20, user_id=second_user.id)
        older = self._message(self.conv1, 10)
        self._message(self.other_conv, 5); self._message(self.cross_conv, 1)
        rows = select_messages(self.db, workspace_id=self.workspace.id, opportunity_id=self.opp.id)
        self.assertEqual([row.id for row in rows], [older.id, newer.id])
        prepared = prepare_communication_input(rows, max_chars=5000)
        self.assertEqual(prepared.message_count_included, 2)
        self.assertNotIn("secret-mailbox", prepared.text)
        self.assertNotIn("secret-thread", prepared.text)
        self.assertNotIn("https://secret", prepared.text)

    def test_cleaning_and_deterministic_newest_preserving_bound(self):
        self.assertEqual(clean_message_body("Hello   team\n\nOn Monday Pat wrote:\n> old"), "Hello team")
        self.assertEqual(clean_message_body("<div>Hello team</div><div>On Monday Pat wrote:</div><blockquote>old</blockquote>", "html"), "Hello team")
        rows = [self._message(self.conv1, minute, body=(f"message {minute} " * 20)) for minute in (1, 2, 3)]
        first = prepare_communication_input(rows, max_chars=500)
        second = prepare_communication_input(rows, max_chars=500)
        self.assertEqual(first, second)
        self.assertIn("message 3", first.text)
        self.assertEqual(first.message_count_available, 3)
        self.assertEqual(first.message_count_included, 2)
        self.assertIn("message 1", first.text)

    def test_persistence_refresh_staleness_and_failed_refresh_preserves_success(self):
        self._message(self.conv1, 1)
        row = generate_and_save_summary(self.db, workspace=self.workspace, opportunity=self.opp, user=self.user, generator=FakeGenerator())
        row_id = row.id
        self.assertFalse(summary_is_stale(self.db, row))
        self._message(self.conv2, 2)
        self.assertTrue(summary_is_stale(self.db, row))
        refreshed = generate_and_save_summary(self.db, workspace=self.workspace, opportunity=self.opp, user=self.user, generator=FakeGenerator())
        self.assertEqual(refreshed.id, row_id)
        self.assertEqual(refreshed.message_count_available, 2)
        self.assertFalse(summary_is_stale(self.db, refreshed))
        with self.assertRaises(CommunicationSummaryError):
            generate_and_save_summary(self.db, workspace=self.workspace, opportunity=self.opp, user=self.user, generator=FakeGenerator(fail=True))
        self.db.refresh(refreshed)
        self.assertEqual(refreshed.status, "ready")
        self.assertEqual(refreshed.current_status, "Awaiting reply")
        self.assertEqual(refreshed.last_error, "timeout")

    def test_csrf_is_bound_to_user_and_opportunity(self):
        token = csrf_token(self.user.id, self.opp.id)
        self.assertTrue(validate_csrf_token(token, self.user.id, self.opp.id))
        self.assertFalse(validate_csrf_token(token, self.user.id, self.other_opp.id))
        self.assertFalse(validate_csrf_token("bad", self.user.id, self.opp.id))


if __name__ == "__main__": unittest.main()
