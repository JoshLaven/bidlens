import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import JobRun, Organization
from bidlens.services.job_runs import (
    JOB_STATUS_FAILED,
    JOB_STATUS_PARTIAL_SUCCESS,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCESS,
    JOB_TYPE_DAILY_SNAPSHOT,
    JOB_TYPE_GRANTS_INGEST,
    JOB_TYPE_SAM_INGEST,
    TRIGGER_TYPE_MANUAL,
    TRIGGER_TYPE_SCHEDULED,
    complete_job_run,
    fail_job_run,
    start_job_run,
)


class JobRunTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Job Run Org", slug="job-run-org")
        self.other_org = Organization(name="Other Job Org", slug="other-job-org")
        self.db.add_all([self.org, self.other_org])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_start_job_run_creates_durable_running_record(self):
        run = start_job_run(
            self.db,
            organization_id=self.org.id,
            job_type=JOB_TYPE_SAM_INGEST,
            trigger_type=TRIGGER_TYPE_SCHEDULED,
            details={"requested_window_days": 7},
        )

        stored = self.db.query(JobRun).filter(JobRun.id == run.id).one()
        self.assertEqual(stored.organization_id, self.org.id)
        self.assertEqual(stored.job_type, JOB_TYPE_SAM_INGEST)
        self.assertEqual(stored.trigger_type, TRIGGER_TYPE_SCHEDULED)
        self.assertEqual(stored.status, JOB_STATUS_RUNNING)
        self.assertEqual(stored.details_json["requested_window_days"], 7)
        self.assertIsNotNone(stored.started_at)

    def test_complete_job_run_records_terminal_state_and_details(self):
        run = start_job_run(
            self.db,
            organization_id=self.org.id,
            job_type=JOB_TYPE_GRANTS_INGEST,
            trigger_type=TRIGGER_TYPE_MANUAL,
        )

        complete_job_run(
            self.db,
            run,
            status=JOB_STATUS_PARTIAL_SUCCESS,
            summary="Completed with detail lookup warnings",
            details={"records_seen": 80, "created": 0, "updated": 1, "errors": 2},
        )

        stored = self.db.query(JobRun).filter(JobRun.id == run.id).one()
        self.assertEqual(stored.status, JOB_STATUS_PARTIAL_SUCCESS)
        self.assertEqual(stored.summary, "Completed with detail lookup warnings")
        self.assertEqual(stored.details_json["records_seen"], 80)
        self.assertEqual(stored.details_json["errors"], 2)
        self.assertIsNotNone(stored.finished_at)
        self.assertIsNotNone(stored.duration_ms)
        self.assertIsNone(stored.error_type)
        self.assertIsNone(stored.error_message)

    def test_fail_job_run_records_safe_error_information(self):
        run = start_job_run(
            self.db,
            organization_id=self.org.id,
            job_type=JOB_TYPE_SAM_INGEST,
            trigger_type=TRIGGER_TYPE_SCHEDULED,
        )

        fail_job_run(
            self.db,
            run,
            RuntimeError(
                "Request failed with api_key=abc123 token=my-token "
                "url=postgresql://user:secret@example.com/db sk-testsecretvalue"
            ),
            summary="SAM.gov ingestion failed",
            details={"records_seen": 0, "errors": 1},
        )

        stored = self.db.query(JobRun).filter(JobRun.id == run.id).one()
        self.assertEqual(stored.status, JOB_STATUS_FAILED)
        self.assertEqual(stored.error_type, "RuntimeError")
        self.assertIn("[redacted]", stored.error_message)
        self.assertNotIn("abc123", stored.error_message)
        self.assertNotIn("my-token", stored.error_message)
        self.assertNotIn("secret", stored.error_message)
        self.assertNotIn("sk-testsecretvalue", stored.error_message)
        self.assertEqual(stored.details_json["errors"], 1)

    def test_runs_are_scoped_by_workspace_organization(self):
        start_job_run(
            self.db,
            organization_id=self.org.id,
            job_type=JOB_TYPE_SAM_INGEST,
            trigger_type=TRIGGER_TYPE_SCHEDULED,
        )
        start_job_run(
            self.db,
            organization_id=self.other_org.id,
            job_type=JOB_TYPE_SAM_INGEST,
            trigger_type=TRIGGER_TYPE_SCHEDULED,
        )

        org_runs = self.db.query(JobRun).filter(JobRun.organization_id == self.org.id).all()

        self.assertEqual(len(org_runs), 1)
        self.assertEqual(org_runs[0].organization_id, self.org.id)

    def test_daily_snapshot_aggregate_details_can_be_stored(self):
        run = start_job_run(
            self.db,
            organization_id=self.org.id,
            job_type=JOB_TYPE_DAILY_SNAPSHOT,
            trigger_type=TRIGGER_TYPE_SCHEDULED,
        )

        complete_job_run(
            self.db,
            run,
            status=JOB_STATUS_SUCCESS,
            summary="Created daily snapshots",
            details={
                "users_eligible": 42,
                "snapshots_created": 40,
                "already_existed": 1,
                "failed": 1,
            },
        )

        stored = self.db.query(JobRun).filter(JobRun.id == run.id).one()
        self.assertEqual(stored.job_type, JOB_TYPE_DAILY_SNAPSHOT)
        self.assertEqual(stored.details_json["users_eligible"], 42)
        self.assertEqual(stored.details_json["snapshots_created"], 40)
        self.assertEqual(stored.details_json["already_existed"], 1)
        self.assertEqual(stored.details_json["failed"], 1)


if __name__ == "__main__":
    unittest.main()
