import datetime as dt
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import (
    DailyBriefEmailDelivery,
    DailySnapshot,
    JobRun,
    Organization,
    OrganizationMembership,
    User,
    Workspace,
)
from bidlens.services import operational_jobs
from bidlens.services.email_delivery import EmailSendResult
from bidlens.services.job_runs import (
    JOB_STATUS_FAILED,
    JOB_STATUS_PARTIAL_SUCCESS,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SKIPPED,
    JOB_STATUS_SUCCESS,
    JOB_TYPE_DAILY_BRIEF_EMAIL,
)
from bidlens.jobs import run_daily_brief_emails


class FakeSender:
    provider = "fake"

    def __init__(self, *, fail_for: set[str] | None = None):
        self.fail_for = fail_for or set()
        self.messages = []

    def send(self, message):
        self.messages.append(message)
        if message.to_email in self.fail_for:
            raise RuntimeError("provider token=secret failed")
        return EmailSendResult(provider=self.provider, message_id=f"msg-{len(self.messages)}")


class InspectingSender(FakeSender):
    def __init__(self, session_factory):
        super().__init__()
        self.session_factory = session_factory
        self.observed_job_status = None
        self.observed_delivery_status = None

    def send(self, message):
        db = self.session_factory()
        try:
            self.observed_job_status = db.query(JobRun).filter(
                JobRun.job_type == JOB_TYPE_DAILY_BRIEF_EMAIL,
            ).one().status
            self.observed_delivery_status = db.query(DailyBriefEmailDelivery).one().status
        finally:
            db.close()
        return super().send(message)


class DailyBriefEmailDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.snapshot_date = dt.date(2026, 7, 22)
        self.org = Organization(name="Brief Org", slug="brief-org", is_active=True, is_live=True)
        self.db.add(self.org)
        self.db.flush()
        self.workspace = Workspace(
            organization_id=self.org.id,
            name="Brief Workspace",
            slug="brief-workspace",
        )
        self.db.add(self.workspace)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _user(self, email="member@example.com", *, name="Morgan Member", org=None, opted_out=False):
        org = org or self.org
        user = User(
            email=email,
            name=name,
            organization_id=org.id,
            daily_brief_email_opted_out=opted_out,
        )
        self.db.add(user)
        self.db.flush()
        self.db.add(OrganizationMembership(
            organization_id=org.id,
            user_id=user.id,
            role="member",
        ))
        self.db.commit()
        return user

    def _snapshot(self, user, *, new_feed=True, extra_activity=True):
        feed_items = []
        if new_feed:
            feed_items = [
                {
                    "id": 101,
                    "title": "New Feed Opportunity",
                    "agency": "Health Agency",
                    "source_label": "SAM.gov",
                    "response_deadline": "2026-08-12",
                }
            ]
        payload = {
            "summary": {"new_feed_count": len(feed_items), "team_signal_count": 1},
            "new_feed_opportunities": feed_items,
            "team_signals": [
                {"title": "Teammate activity should stay out of email"},
            ] if extra_activity else [],
            "shortlist_changes": [
                {"title": "Shortlist change should stay out of email"},
            ] if extra_activity else [],
            "connector_issues": [
                {"source_label": "Source issue should stay out of email"},
            ] if extra_activity else [],
        }
        self.db.add(DailySnapshot(
            workspace_id=self.workspace.id,
            user_id=user.id,
            snapshot_date=self.snapshot_date,
            status="completed",
            snapshot_json=payload,
        ))
        self.db.commit()

    def test_eligible_user_receives_one_feed_only_email(self):
        user = self._user()
        self._snapshot(user)
        sender = FakeSender()

        exit_code = operational_jobs.run_daily_brief_emails_job(
            session_factory=self.Session,
            snapshot_date=self.snapshot_date,
            email_sender=sender,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(sender.messages), 1)
        message = sender.messages[0]
        self.assertEqual(message.to_email, user.email)
        self.assertIn("Morgan's BidLens Daily Brief", message.subject)
        self.assertIn("New Feed Opportunity", message.html_body)
        self.assertIn("Health Agency", message.html_body)
        self.assertIn("Open your BidLens Feed", message.html_body)
        self.assertNotIn("Teammate activity", message.html_body)
        self.assertNotIn("Shortlist change", message.html_body)
        self.assertNotIn("Source issue", message.html_body)
        delivery = self.db.query(DailyBriefEmailDelivery).one()
        self.assertEqual(delivery.status, JOB_STATUS_SUCCESS)
        self.assertEqual(delivery.item_count, 1)
        self.assertEqual(delivery.provider_message_id, "msg-1")
        run = self.db.query(JobRun).filter(JobRun.job_type == JOB_TYPE_DAILY_BRIEF_EMAIL).one()
        self.assertEqual(run.status, JOB_STATUS_SUCCESS)
        self.assertEqual(run.details_json["sent"], 1)

    def test_running_job_and_delivery_status_are_available_before_send(self):
        user = self._user()
        self._snapshot(user)
        sender = InspectingSender(self.Session)

        exit_code = operational_jobs.run_daily_brief_emails_job(
            session_factory=self.Session,
            snapshot_date=self.snapshot_date,
            email_sender=sender,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(sender.observed_job_status, JOB_STATUS_RUNNING)
        self.assertEqual(sender.observed_delivery_status, JOB_STATUS_RUNNING)

    def test_non_live_and_inactive_organizations_are_skipped(self):
        inactive_org = Organization(name="Inactive", slug="inactive", is_active=False, is_live=True)
        prelive_org = Organization(name="Prelive", slug="prelive", is_active=True, is_live=False)
        self.db.add_all([inactive_org, prelive_org])
        self.db.flush()
        for org in (inactive_org, prelive_org):
            workspace = Workspace(organization_id=org.id, name=f"{org.name} Workspace", slug=f"{org.slug}-workspace")
            self.db.add(workspace)
            self.db.flush()
            user = User(email=f"{org.slug}@example.com", organization_id=org.id)
            self.db.add(user)
            self.db.flush()
            self.db.add(OrganizationMembership(organization_id=org.id, user_id=user.id, role="member"))
            self.db.add(DailySnapshot(
                workspace_id=workspace.id,
                user_id=user.id,
                snapshot_date=self.snapshot_date,
                status="completed",
                snapshot_json={"summary": {"new_feed_count": 1}, "new_feed_opportunities": [{"title": "Hidden"}]},
            ))
        self.db.commit()
        sender = FakeSender()

        exit_code = operational_jobs.run_daily_brief_emails_job(
            session_factory=self.Session,
            snapshot_date=self.snapshot_date,
            email_sender=sender,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(sender.messages, [])
        run = self.db.query(JobRun).filter(JobRun.job_type == JOB_TYPE_DAILY_BRIEF_EMAIL).one()
        self.assertEqual(run.organization_id, self.org.id)
        self.assertEqual(run.status, JOB_STATUS_SKIPPED)

    def test_invalid_email_opt_out_and_missing_snapshot_skip_safely(self):
        invalid = self._user("not-an-email")
        opted_out = self._user("opted@example.com", opted_out=True)
        no_snapshot = self._user("nosnapshot@example.com")
        self._snapshot(invalid)
        self._snapshot(opted_out)
        sender = FakeSender()

        exit_code = operational_jobs.run_daily_brief_emails_job(
            session_factory=self.Session,
            snapshot_date=self.snapshot_date,
            email_sender=sender,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(sender.messages, [])
        self.assertEqual(self.db.query(DailyBriefEmailDelivery).count(), 1)
        delivery = self.db.query(DailyBriefEmailDelivery).one()
        self.assertEqual(delivery.user_id, no_snapshot.id)
        self.assertEqual(delivery.status, JOB_STATUS_SKIPPED)

    def test_no_new_opportunities_sends_concise_fallback(self):
        user = self._user()
        self._snapshot(user, new_feed=False)
        sender = FakeSender()

        exit_code = operational_jobs.run_daily_brief_emails_job(
            session_factory=self.Session,
            snapshot_date=self.snapshot_date,
            email_sender=sender,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(sender.messages), 1)
        self.assertIn("No new Feed opportunities were added yesterday.", sender.messages[0].text_body)

    def test_successful_delivery_is_not_duplicated_on_rerun(self):
        user = self._user()
        self._snapshot(user)
        sender = FakeSender()

        operational_jobs.run_daily_brief_emails_job(
            session_factory=self.Session,
            snapshot_date=self.snapshot_date,
            email_sender=sender,
        )
        operational_jobs.run_daily_brief_emails_job(
            session_factory=self.Session,
            snapshot_date=self.snapshot_date,
            email_sender=sender,
        )

        self.assertEqual(len(sender.messages), 1)
        self.assertEqual(self.db.query(DailyBriefEmailDelivery).count(), 1)

    def test_failed_delivery_can_be_retried_and_one_failure_does_not_stop_later_users(self):
        first = self._user("first@example.com", name="First")
        second = self._user("second@example.com", name="Second")
        self._snapshot(first)
        self._snapshot(second)
        failing_sender = FakeSender(fail_for={"first@example.com"})

        exit_code = operational_jobs.run_daily_brief_emails_job(
            session_factory=self.Session,
            snapshot_date=self.snapshot_date,
            email_sender=failing_sender,
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(len(failing_sender.messages), 2)
        deliveries = {
            row.recipient_email: row.status
            for row in self.db.query(DailyBriefEmailDelivery).all()
        }
        self.assertEqual(deliveries["first@example.com"], JOB_STATUS_FAILED)
        self.assertEqual(deliveries["second@example.com"], JOB_STATUS_SUCCESS)
        run = self.db.query(JobRun).filter(JobRun.job_type == JOB_TYPE_DAILY_BRIEF_EMAIL).one()
        self.assertEqual(run.status, JOB_STATUS_PARTIAL_SUCCESS)
        self.assertNotIn("secret", self.db.query(DailyBriefEmailDelivery).filter_by(recipient_email="first@example.com").one().error_message)

        retry_sender = FakeSender()
        retry_exit = operational_jobs.run_daily_brief_emails_job(
            session_factory=self.Session,
            snapshot_date=self.snapshot_date,
            email_sender=retry_sender,
        )
        self.assertEqual(retry_exit, 0)
        self.assertEqual(len(retry_sender.messages), 1)
        self.assertEqual(retry_sender.messages[0].to_email, "first@example.com")


class DailyBriefEmailCommandTests(unittest.TestCase):
    def test_cli_exits_zero_when_operational_job_succeeds(self):
        with patch("bidlens.jobs.run_daily_brief_emails._run_operational_job", return_value=0) as run_job:
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = run_daily_brief_emails.run([])

        self.assertEqual(exit_code, 0)
        run_job.assert_called_once_with(trigger_type="scheduled", snapshot_date=None)
        self.assertIn("completed successfully", output.getvalue())

    def test_cli_exits_zero_for_isolated_failures(self):
        with patch("bidlens.jobs.run_daily_brief_emails._run_operational_job", return_value=1):
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = run_daily_brief_emails.run([])

        self.assertEqual(exit_code, 0)
        self.assertIn("isolated delivery failures", output.getvalue())

    def test_cli_exits_nonzero_for_job_level_exception(self):
        with patch("bidlens.jobs.run_daily_brief_emails._run_operational_job", side_effect=RuntimeError("database unavailable")):
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = run_daily_brief_emails.run([])

        self.assertEqual(exit_code, 1)
        self.assertIn("failed before completion: RuntimeError", output.getvalue())

    def test_cli_does_not_import_fastapi_or_scheduler_in_wrapper(self):
        source = open("src/bidlens/jobs/run_daily_brief_emails.py").read()
        self.assertNotIn("bidlens.main", source)
        self.assertNotIn("start_scheduler", source)
        self.assertNotIn("scheduler", source)


if __name__ == "__main__":
    unittest.main()
