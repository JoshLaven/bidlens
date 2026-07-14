import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens import scheduler
from bidlens.database import Base
from bidlens.models import GrantsSourceConfig, Organization, OrganizationMembership, User
from bidlens.routes import grants


class GrantsSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.enabled_org = Organization(name="Enabled Grants", slug="enabled-grants")
        self.disabled_org = Organization(name="Disabled Grants", slug="disabled-grants")
        self.db.add_all([self.enabled_org, self.disabled_org])
        self.db.flush()
        self.admin = User(email="admin@grants.test", organization_id=self.enabled_org.id)
        self.member = User(email="member@grants.test", organization_id=self.enabled_org.id)
        self.db.add_all([self.admin, self.member])
        self.db.flush()
        self.db.add_all([
            OrganizationMembership(
                organization_id=self.enabled_org.id,
                user_id=self.admin.id,
                role="admin",
            ),
            OrganizationMembership(
                organization_id=self.enabled_org.id,
                user_id=self.member.id,
                role="member",
            ),
            GrantsSourceConfig(
                organization_id=self.enabled_org.id,
                enabled=True,
                posted_days_back=7,
                rows=25,
            ),
            GrantsSourceConfig(
                organization_id=self.disabled_org.id,
                enabled=False,
                posted_days_back=7,
                rows=25,
            ),
        ])
        self.db.commit()
        self.enabled_org_id = self.enabled_org.id
        self.disabled_org_id = self.disabled_org.id
        self.admin_id = self.admin.id
        self.member_id = self.member.id

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _request(self):
        return SimpleNamespace(query_params={"org_id": str(self.enabled_org_id)})

    def test_grants_scheduler_delegates_to_standalone_job_orchestration(self):
        with patch("bidlens.scheduler.run_grants_ingest_job", return_value=0) as run_job:
            scheduler.run_grants_ingest()

        run_job.assert_called_once_with()

    def test_sam_scheduler_delegates_to_standalone_job_orchestration(self):
        with patch("bidlens.scheduler.run_sam_ingest_job", return_value=0) as run_job:
            scheduler.run_sam_ingest()

        run_job.assert_called_once_with()

    def test_manual_pull_is_blocked_for_non_admin_members(self):
        setattr(self.member, "current_organization_id", self.enabled_org_id)
        with patch("bidlens.routes.grants.get_current_user", return_value=self.member):
            response = grants.pull_now(self._request(), user=self.member, db=self.db)

        self.assertEqual(response.status_code, 403)

    def test_scheduler_still_registers_sam_job(self):
        fake_scheduler = Mock()
        with patch("bidlens.scheduler.BackgroundScheduler", return_value=fake_scheduler):
            returned = scheduler.start_scheduler()

        self.assertIs(returned, fake_scheduler)
        self.assertEqual(fake_scheduler.add_job.call_count, 2)
        scheduled_functions = [call.args[0] for call in fake_scheduler.add_job.call_args_list]
        self.assertIn(scheduler.run_sam_ingest, scheduled_functions)
        self.assertIn(scheduler.run_grants_ingest, scheduled_functions)
        fake_scheduler.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
