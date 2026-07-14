import datetime as dt
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from bidlens.database import Base
from bidlens.models import (
    DailySnapshot,
    GrantsSourceConfig,
    JobRun,
    Organization,
    OrganizationMembership,
    SamSourceConfig,
    User,
    Workspace,
)
from bidlens.services import operational_jobs
from bidlens.services.job_runs import (
    JOB_STATUS_FAILED,
    JOB_STATUS_PARTIAL_SUCCESS,
    JOB_STATUS_PAUSED,
    JOB_STATUS_SKIPPED,
    JOB_STATUS_SUCCESS,
    JOB_TYPE_DAILY_SNAPSHOT,
    JOB_TYPE_GRANTS_INGEST,
    JOB_TYPE_SAM_INGEST,
)


class TrackingSession(Session):
    close_count = 0

    def close(self):
        type(self).close_count += 1
        super().close()


class OperationalJobTests(unittest.TestCase):
    def setUp(self):
        TrackingSession.close_count = 0
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, class_=TrackingSession)
        self.db = self.Session()
        self.org = Organization(name="Job Org", slug="job-org")
        self.other_org = Organization(name="Other Job Org", slug="other-job-org")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.org_id = self.org.id
        self.other_org_id = self.other_org.id

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _sam_config(self, organization_id, *, name="Default SAM"):
        config = SamSourceConfig(
            organization_id=organization_id,
            name=name,
            naics_codes=["541611"],
            keywords=[],
            agencies=[],
            set_asides=[],
            notice_types=["Solicitation"],
            posted_days_back=7,
            active_only=True,
            max_records=25,
        )
        self.db.add(config)
        self.db.commit()
        return config

    def _grants_config(self, organization_id, *, enabled=True):
        config = GrantsSourceConfig(
            organization_id=organization_id,
            enabled=enabled,
            posted_days_back=7,
            rows=25,
        )
        self.db.add(config)
        self.db.commit()
        return config

    def _workspace_with_users(self, organization_id, user_count):
        workspace = Workspace(
            organization_id=organization_id,
            name=f"Workspace {organization_id}",
            slug=f"workspace-{organization_id}",
        )
        self.db.add(workspace)
        self.db.flush()
        users = []
        for index in range(user_count):
            user = User(
                email=f"user-{organization_id}-{index}@example.com",
                name=f"User {index}",
                organization_id=organization_id,
            )
            self.db.add(user)
            self.db.flush()
            self.db.add(OrganizationMembership(
                organization_id=organization_id,
                user_id=user.id,
                role="member",
            ))
            users.append(user)
        self.db.commit()
        return workspace, users

    def test_sam_success_records_job_run_details(self):
        self._sam_config(self.org_id)
        result = {
            "status": "success",
            "run_id": 101,
            "records_seen": 49,
            "inserted": 3,
            "updated": 0,
            "unchanged": 2,
            "skipped": 0,
            "filtered": 46,
            "errors": 0,
            "pages_pulled": 2,
            "search_requests_made": 2,
        }

        with patch("bidlens.services.operational_jobs.ingest_sam", return_value=result):
            exit_code = operational_jobs.run_sam_ingest_job(session_factory=self.Session)

        self.assertEqual(exit_code, 0)
        run = self.db.query(JobRun).filter(JobRun.job_type == JOB_TYPE_SAM_INGEST).one()
        self.assertEqual(run.organization_id, self.org_id)
        self.assertEqual(run.status, JOB_STATUS_SUCCESS)
        self.assertEqual(run.details_json["records_seen"], 49)
        self.assertEqual(run.details_json["created"], 3)
        self.assertEqual(run.details_json["filtered"], 46)
        self.assertEqual(run.details_json["ingestion_run_ids"], [101])

    def test_sam_paused_rate_limit_is_acceptable_exit(self):
        self._sam_config(self.org_id)
        result = {
            "status": "paused_rate_limit",
            "run_id": 102,
            "records_seen": 49,
            "inserted": 3,
            "updated": 0,
            "unchanged": 0,
            "skipped": 0,
            "filtered": 46,
            "errors": 0,
            "stopped_due_to_rate_limit": True,
        }

        with patch("bidlens.services.operational_jobs.ingest_sam", return_value=result):
            exit_code = operational_jobs.run_sam_ingest_job(session_factory=self.Session)

        self.assertEqual(exit_code, 0)
        run = self.db.query(JobRun).filter(JobRun.job_type == JOB_TYPE_SAM_INGEST).one()
        self.assertEqual(run.status, JOB_STATUS_PAUSED)
        self.assertTrue(run.details_json["checkpoint_saved"])
        self.assertEqual(run.details_json["pause_reason"], "rate_limit")

    def test_sam_failed_organization_does_not_stop_next_organization(self):
        self._sam_config(self.org_id)
        self._sam_config(self.other_org_id)
        success = {
            "status": "success",
            "run_id": 103,
            "records_seen": 1,
            "inserted": 1,
            "updated": 0,
            "unchanged": 0,
            "skipped": 0,
            "filtered": 0,
            "errors": 0,
        }

        with patch("bidlens.services.operational_jobs.ingest_sam", side_effect=[RuntimeError("api_key=secret"), success]):
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = operational_jobs.run_sam_ingest_job(session_factory=self.Session)

        self.assertEqual(exit_code, 1)
        self.assertNotIn("secret", output.getvalue())
        runs = self.db.query(JobRun).filter(JobRun.job_type == JOB_TYPE_SAM_INGEST).order_by(JobRun.organization_id).all()
        self.assertEqual([run.status for run in runs], [JOB_STATUS_FAILED, JOB_STATUS_SUCCESS])

    def test_grants_success_records_job_run_and_ingestion_run(self):
        self._grants_config(self.org_id)
        result = {
            "status": "success",
            "received": 80,
            "created": 0,
            "updated": 1,
            "unchanged": 79,
            "skipped": 0,
            "errors": 0,
            "detail_errors": 0,
            "pages_pulled": 4,
            "requested_date_window": "7 days",
            "message": "Grants.gov pull completed.",
        }

        with patch("bidlens.services.operational_jobs.ingest_grants_gov", return_value=result):
            exit_code = operational_jobs.run_grants_ingest_job(session_factory=self.Session)

        self.assertEqual(exit_code, 0)
        run = self.db.query(JobRun).filter(JobRun.job_type == JOB_TYPE_GRANTS_INGEST).one()
        self.assertEqual(run.status, JOB_STATUS_SUCCESS)
        self.assertEqual(run.details_json["records_seen"], 80)
        self.assertEqual(run.details_json["updated"], 1)
        self.assertEqual(len(run.details_json["ingestion_run_ids"]), 1)

    def test_grants_no_records_is_skipped_and_successful_exit(self):
        self._grants_config(self.org_id)
        result = {
            "status": "no_records",
            "received": 0,
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "skipped": 0,
            "errors": 0,
            "detail_errors": 0,
            "pages_pulled": 1,
            "requested_date_window": "7 days",
            "message": "Completed - no records returned.",
        }

        with patch("bidlens.services.operational_jobs.ingest_grants_gov", return_value=result):
            exit_code = operational_jobs.run_grants_ingest_job(session_factory=self.Session)

        self.assertEqual(exit_code, 0)
        run = self.db.query(JobRun).filter(JobRun.job_type == JOB_TYPE_GRANTS_INGEST).one()
        self.assertEqual(run.status, JOB_STATUS_SKIPPED)
        self.assertEqual(run.details_json["records_seen"], 0)

    def test_grants_failure_isolated_from_next_organization(self):
        self._grants_config(self.org_id)
        self._grants_config(self.other_org_id)
        success = {
            "status": "success",
            "received": 1,
            "created": 1,
            "updated": 0,
            "unchanged": 0,
            "skipped": 0,
            "errors": 0,
            "detail_errors": 0,
            "pages_pulled": 1,
            "message": "ok",
        }

        with patch("bidlens.services.operational_jobs.ingest_grants_gov", side_effect=[RuntimeError("token=secret"), success]):
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = operational_jobs.run_grants_ingest_job(session_factory=self.Session)

        self.assertEqual(exit_code, 1)
        self.assertNotIn("secret", output.getvalue())
        runs = self.db.query(JobRun).filter(JobRun.job_type == JOB_TYPE_GRANTS_INGEST).order_by(JobRun.organization_id).all()
        self.assertEqual([run.status for run in runs], [JOB_STATUS_FAILED, JOB_STATUS_SUCCESS])

    def test_daily_snapshots_aggregate_counts_existing_and_created(self):
        workspace, users = self._workspace_with_users(self.org_id, 2)
        snapshot_date = dt.date(2026, 7, 13)
        self.db.add(DailySnapshot(
            workspace_id=workspace.id,
            user_id=users[0].id,
            snapshot_date=snapshot_date,
            status="completed",
            snapshot_json={"existing": True},
        ))
        self.db.commit()

        exit_code = operational_jobs.run_daily_snapshots_job(
            session_factory=self.Session,
            snapshot_date=snapshot_date,
        )

        self.assertEqual(exit_code, 0)
        run = self.db.query(JobRun).filter(JobRun.job_type == JOB_TYPE_DAILY_SNAPSHOT).one()
        self.assertEqual(run.status, JOB_STATUS_SUCCESS)
        self.assertEqual(run.details_json["users_eligible"], 2)
        self.assertEqual(run.details_json["already_existed"], 1)
        self.assertEqual(run.details_json["snapshots_created"], 1)

    def test_daily_snapshots_partial_failure(self):
        self._workspace_with_users(self.org_id, 2)
        snapshot_date = dt.date(2026, 7, 13)
        original = operational_jobs.create_daily_snapshot

        def flaky_create(*args, **kwargs):
            if kwargs["user_id"] % 2 == 0:
                raise RuntimeError("snapshot failed")
            return original(*args, **kwargs)

        with patch("bidlens.services.operational_jobs.create_daily_snapshot", side_effect=flaky_create):
            exit_code = operational_jobs.run_daily_snapshots_job(
                session_factory=self.Session,
                snapshot_date=snapshot_date,
            )

        self.assertEqual(exit_code, 1)
        run = self.db.query(JobRun).filter(JobRun.job_type == JOB_TYPE_DAILY_SNAPSHOT).one()
        self.assertEqual(run.status, JOB_STATUS_PARTIAL_SUCCESS)
        self.assertEqual(run.details_json["failed"], 1)
        self.assertEqual(run.details_json["snapshots_created"], 1)

    def test_daily_snapshots_no_eligible_users_is_skipped(self):
        self._workspace_with_users(self.org_id, 0)

        exit_code = operational_jobs.run_daily_snapshots_job(
            session_factory=self.Session,
            snapshot_date=dt.date(2026, 7, 13),
        )

        self.assertEqual(exit_code, 0)
        run = self.db.query(JobRun).filter(JobRun.job_type == JOB_TYPE_DAILY_SNAPSHOT).one()
        self.assertEqual(run.status, JOB_STATUS_SKIPPED)
        self.assertEqual(run.details_json["users_eligible"], 0)


if __name__ == "__main__":
    unittest.main()
