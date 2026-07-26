import contextlib
import datetime as dt
import io
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens import scheduler
from bidlens.database import Base
from bidlens.models import (
    ExternalIntegrationConnection,
    Opportunity,
    OpportunityConversation,
    Organization,
    User,
    Workspace,
)
from bidlens.services.outlook_sync_jobs import run_outlook_conversation_sync_job


class OutlookSyncOperationalJobTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _workspace(self, slug, *, live=True, connection_status="connected", tracked=True):
        org = Organization(name=slug, slug=slug, is_live=live)
        self.db.add(org)
        self.db.flush()
        workspace = Workspace(organization_id=org.id, name=slug, slug=slug)
        user = User(email=f"{slug}@example.com", organization_id=org.id)
        self.db.add_all([workspace, user])
        self.db.flush()
        opportunity = Opportunity(
            organization_id=org.id, source="sam", source_record_id=f"opp-{slug}", title=slug,
            agency="Agency", opportunity_type="Solicitation", posted_date=dt.date(2026, 7, 1),
            response_deadline=dt.date(2026, 8, 1), description="Description",
        )
        self.db.add(opportunity)
        self.db.flush()
        self.db.add(ExternalIntegrationConnection(
            workspace_id=workspace.id, user_id=user.id, provider="microsoft",
            connection_status=connection_status, external_user_id=f"mailbox-{slug}",
            connected_email=user.email, encrypted_access_token="encrypted", granted_scopes="Mail.ReadWrite",
        ))
        if tracked:
            self.db.add(OpportunityConversation(
                workspace_id=workspace.id, opportunity_id=opportunity.id, provider="microsoft",
                external_conversation_id=f"thread-{slug}", initial_provider_message_id=f"message-{slug}",
                provider_mailbox_id=f"mailbox-{slug}", started_by_user_id=user.id,
                tracking_status="tracked", subject="Tracked",
            ))
        self.db.commit()
        return workspace

    @patch("bidlens.services.outlook_sync_jobs.sync_tracked_microsoft_conversations")
    def test_only_live_tracked_workspace_with_usable_connection_is_processed(self, sync):
        eligible = self._workspace("eligible")
        self._workspace("disconnected", connection_status="disconnected")
        self._workspace("reauth", connection_status="reauthorization_required")
        self._workspace("no-tracked", tracked=False)
        self._workspace("inactive", live=False)
        sync.return_value = {
            "conversations_checked": 2, "conversations_succeeded": 2, "conversations_failed": 0,
            "new_messages_imported": 3, "duplicates_skipped": 1, "messages_skipped": 0,
            "reauthorization_required": 0,
        }

        result = run_outlook_conversation_sync_job(session_factory=self.Session)

        self.assertEqual(result["workspaces_considered"], 3)
        self.assertEqual(result["workspaces_synced"], 1)
        self.assertEqual(result["workspaces_skipped"], 2)
        self.assertEqual(result["messages_imported"], 3)
        called_workspace = sync.call_args.kwargs["workspace"]
        self.assertEqual(called_workspace.id, eligible.id)
        self.assertTrue(sync.call_args.kwargs["stop_on_authorization_failure"])

    @patch("bidlens.services.outlook_sync_jobs.sync_tracked_microsoft_conversations")
    def test_workspace_failure_is_isolated_and_log_omits_exception_message(self, sync):
        self._workspace("first")
        self._workspace("second")
        sync.side_effect = [RuntimeError("token=top-secret subject=Sensitive"), {
            "conversations_checked": 1, "conversations_succeeded": 1, "conversations_failed": 0,
            "new_messages_imported": 1, "duplicates_skipped": 0, "messages_skipped": 0,
            "reauthorization_required": 0,
        }]
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = run_outlook_conversation_sync_job(session_factory=self.Session)

        self.assertEqual(result["workspaces_failed"], 1)
        self.assertEqual(result["workspaces_synced"], 1)
        self.assertEqual(result["messages_imported"], 1)
        self.assertNotIn("top-secret", output.getvalue())
        self.assertNotIn("Sensitive", output.getvalue())

    @patch("bidlens.services.outlook_sync_jobs.sync_tracked_microsoft_conversations")
    def test_zero_workspace_run_succeeds_without_calling_shared_service(self, sync):
        result = run_outlook_conversation_sync_job(session_factory=self.Session)
        self.assertEqual(result["workspaces_considered"], 0)
        self.assertEqual(result["messages_imported"], 0)
        sync.assert_not_called()


class OutlookSchedulerRegistrationTests(unittest.TestCase):
    def test_outlook_job_delegates_to_operational_wrapper(self):
        with patch("bidlens.scheduler.run_outlook_conversation_sync_job", return_value={"workspaces_synced": 0}) as run_job:
            result = scheduler.run_outlook_conversation_sync()
        self.assertEqual(result, {"workspaces_synced": 0})
        run_job.assert_called_once_with()

    def test_registration_uses_stable_non_overlapping_interval_job(self):
        fake_scheduler = Mock()
        with patch("bidlens.scheduler.BackgroundScheduler", return_value=fake_scheduler):
            scheduler.start_scheduler()

        calls = [call for call in fake_scheduler.add_job.call_args_list if call.kwargs.get("id") == "outlook-conversation-sync"]
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertIs(call.args[0], scheduler.run_outlook_conversation_sync)
        self.assertEqual(call.args[1].interval.total_seconds(), 15 * 60)
        self.assertEqual(call.kwargs["max_instances"], 1)
        self.assertTrue(call.kwargs["coalesce"])
        self.assertEqual(call.kwargs["misfire_grace_time"], 300)
        fake_scheduler.start.assert_called_once()

    def test_ui_describes_automatic_narrow_sync_and_keeps_manual_control(self):
        source = Path("src/bidlens/templates/microsoft_connection.html").read_text(encoding="utf-8")
        self.assertIn("approximately every 15 minutes", source)
        self.assertIn("Only conversations initiated from BidLens", source)
        self.assertIn("Sync Now", source)
        self.assertIn("{% if is_admin %}", source)


if __name__ == "__main__":
    unittest.main()
