import datetime as dt
import io
import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import DailySnapshot, Organization, OrganizationMembership, User, Workspace
from bidlens.services.home import get_daily_brief_home_context
from scripts.seed_home_daily_snapshot import reset_home_snapshot, run, seed_home_snapshot


class HomeDailySnapshotQAHelperTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine)
        self.db = self.factory()
        self.org = Organization(name="Local QA Org", slug="local-qa-org")
        self.db.add(self.org)
        self.db.flush()
        self.workspace = Workspace(
            organization_id=self.org.id, name="Local QA Workspace", slug="local-qa-workspace",
        )
        self.user = User(
            organization_id=self.org.id, name="QA User", email="qa@example.test",
        )
        self.other_user = User(
            organization_id=self.org.id, name="Other User", email="other@example.test",
        )
        self.db.add_all([self.workspace, self.user, self.other_user])
        self.db.flush()
        self.db.add_all([
            OrganizationMembership(
                organization_id=self.org.id, user_id=self.user.id, role="member",
            ),
            OrganizationMembership(
                organization_id=self.org.id, user_id=self.other_user.id, role="member",
            ),
        ])
        self.date = dt.date(2026, 8, 4)
        self.db.add(DailySnapshot(
            workspace_id=self.workspace.id, user_id=self.other_user.id,
            snapshot_date=self.date, status="completed", snapshot_json={"untouched": True},
        ))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_seed_creates_representative_sections_and_preserves_other_user(self):
        snapshot, action = seed_home_snapshot(
            self.db, workspace_id=self.workspace.id, user_id=self.user.id,
            snapshot_date=self.date,
        )

        self.assertEqual(action, "created")
        payload = snapshot.snapshot_json
        self.assertEqual(len(payload["shortlist_deadlines"]), 1)
        self.assertEqual(len(payload["shortlist_updates"]), 1)
        self.assertEqual(len(payload["team_signals"]), 1)
        self.assertEqual(
            payload["shortlist_updates"][0]["subtitle"],
            "Response deadline changed from Aug 18 to Aug 25",
        )
        self.assertEqual(payload["team_signals"][0]["subtitle"], "Kendall Roy showed interest")
        other = self.db.query(DailySnapshot).filter(
            DailySnapshot.user_id == self.other_user.id,
        ).one()
        self.assertEqual(other.snapshot_json, {"untouched": True})

        context = get_daily_brief_home_context(
            self.db, self.org.id, self.user.id,
            now=dt.datetime(2026, 8, 4, 12, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(
            [section["key"] for section in context["shortlist_sections"]],
            ["shortlist_deadlines", "shortlist_updates", "team_signals"],
        )

    def test_seed_replaces_only_selected_existing_snapshot(self):
        self.db.add(DailySnapshot(
            workspace_id=self.workspace.id, user_id=self.user.id,
            snapshot_date=self.date, status="completed", snapshot_json={"old": True},
        ))
        self.db.commit()

        snapshot, action = seed_home_snapshot(
            self.db, workspace_id=self.workspace.id, user_id=self.user.id,
            snapshot_date=self.date,
        )

        self.assertEqual(action, "updated")
        self.assertNotIn("old", snapshot.snapshot_json)
        self.assertEqual(
            self.db.query(DailySnapshot).filter(
                DailySnapshot.workspace_id == self.workspace.id,
                DailySnapshot.user_id == self.user.id,
                DailySnapshot.snapshot_date == self.date,
            ).count(),
            1,
        )

    def test_reset_only_deletes_helper_fixture(self):
        seed_home_snapshot(
            self.db, workspace_id=self.workspace.id, user_id=self.user.id,
            snapshot_date=self.date,
        )
        self.assertTrue(reset_home_snapshot(
            self.db, workspace_id=self.workspace.id, user_id=self.user.id,
            snapshot_date=self.date,
        ))
        self.assertFalse(reset_home_snapshot(
            self.db, workspace_id=self.workspace.id, user_id=self.user.id,
            snapshot_date=self.date,
        ))
        with self.assertRaisesRegex(RuntimeError, "not created by this QA helper"):
            reset_home_snapshot(
                self.db, workspace_id=self.workspace.id, user_id=self.other_user.id,
                snapshot_date=self.date,
            )

    def test_non_sqlite_is_refused_before_queries(self):
        fake_db = SimpleNamespace(
            get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
            rollback=lambda: None,
            close=lambda: None,
        )
        output = io.StringIO()

        status = run(
            workspace_id=1, user_id=1, snapshot_date=self.date,
            session_factory=lambda: fake_db, output=output,
        )

        self.assertEqual(status, 1)
        self.assertIn("requires SQLite", output.getvalue())

    def test_user_must_belong_to_selected_workspace(self):
        other_org = Organization(name="Other Org", slug="other-org")
        self.db.add(other_org)
        self.db.flush()
        outsider = User(
            organization_id=other_org.id, name="Outsider", email="outsider@example.test",
        )
        self.db.add(outsider)
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "is not a member"):
            seed_home_snapshot(
                self.db, workspace_id=self.workspace.id, user_id=outsider.id,
                snapshot_date=self.date,
            )


if __name__ == "__main__":
    unittest.main()
