import datetime as dt
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import JobRun, Organization, User, Workspace
from bidlens.routes import platform


class _Url:
    def __init__(self, path="/platform/operations"):
        self.path = path


class _Request:
    def __init__(self, path="/platform/operations", query_params=None):
        self.url = _Url(path)
        self.query_params = query_params or {}


class PlatformOperationsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.env = Environment(
            loader=FileSystemLoader("src/bidlens/templates"),
            autoescape=select_autoescape(["html"]),
        )
        self.org = Organization(name="NORC", slug="norc")
        self.other_org = Organization(name="Demo Workspace Org", slug="demo-workspace-org")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.workspace = Workspace(organization_id=self.org.id, name="NORC Workspace", slug="norc-workspace")
        self.other_workspace = Workspace(
            organization_id=self.other_org.id,
            name="Demo Workspace",
            slug="demo-workspace",
        )
        self.db.add_all([self.workspace, self.other_workspace])
        self.owner = User(email="joshuatlaven@gmail.com", name="Josh", organization_id=self.org.id)
        self.admin = User(email="admin@example.com", name="Admin", organization_id=self.org.id)
        self.member = User(email="member@example.com", name="Member", organization_id=self.org.id)
        self.db.add_all([self.owner, self.admin, self.member])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _run(self, **overrides):
        values = {
            "organization_id": self.org.id,
            "job_type": "sam_ingest",
            "trigger_type": "scheduled",
            "status": "success",
            "started_at": dt.datetime(2026, 7, 14, 1, 0),
            "finished_at": dt.datetime(2026, 7, 14, 1, 1, 34),
            "duration_ms": 94000,
            "summary": "SAM.gov ingestion success: 49 records seen",
            "details_json": {
                "source_configs_processed": 2,
                "records_seen": 49,
                "created": 3,
                "updated": 0,
                "filtered": 46,
                "errors": 0,
                "checkpoint_saved": True,
                "pause_reason": "rate_limit",
            },
        }
        values.update(overrides)
        run = JobRun(**values)
        self.db.add(run)
        self.db.commit()
        return run

    def _render_list(self, query_params=None):
        context = platform._operations_context(_Request(query_params=query_params), self.db, self.owner)
        return self.env.get_template("platform_operations.html").render(**context), context

    def _render_detail(self, run_id):
        context = platform._operation_detail_context(
            _Request(f"/platform/operations/{run_id}"),
            self.db,
            self.owner,
            run_id,
        )
        return self.env.get_template("platform_operation_detail.html").render(**context), context

    def test_platform_owner_can_access_operations(self):
        with patch.dict(
            "os.environ",
            {
                "PLATFORM_OWNER_EMAIL": self.owner.email,
                "PLATFORM_ADMIN_EMAILS": "",
            },
            clear=False,
        ), patch("bidlens.routes.platform.get_current_user", return_value=self.owner):
            user = platform.require_platform_admin(_Request(), self.db)

        self.assertEqual(user.email, "joshuatlaven@gmail.com")
        self.assertTrue(user.is_platform_admin)

    def test_workspace_admin_and_member_cannot_access_operations(self):
        for user in (self.admin, self.member):
            with patch("bidlens.routes.platform.get_current_user", return_value=user):
                with self.assertRaises(Exception) as raised:
                    platform.require_platform_admin(_Request(), self.db)
            self.assertEqual(getattr(raised.exception, "status_code", None), 404)

    def test_recent_job_runs_render_descending_with_readable_labels(self):
        older = self._run(started_at=dt.datetime(2026, 7, 14, 1, 0), summary="Older run")
        newer = self._run(
            organization_id=self.other_org.id,
            job_type="grants_ingest",
            status="failed",
            started_at=dt.datetime(2026, 7, 14, 1, 30),
            summary="Newer run",
        )

        html, context = self._render_list()

        self.assertEqual([row["run"].id for row in context["runs"]], [newer.id, older.id])
        self.assertIn("Demo Workspace", html)
        self.assertIn("Grants.gov Pull", html)
        self.assertIn("Failed", html)
        self.assertIn("SAM.gov Pull", html)
        self.assertIn("Success", html)
        self.assertIn("/platform/operations", html)
        self.assertNotIn("Workspace Management", html)

    def test_filters_work_together(self):
        matching = self._run(
            organization_id=self.org.id,
            job_type="daily_snapshot",
            status="partial_success",
            started_at=dt.datetime(2026, 7, 14, 3, 0),
        )
        self._run(
            organization_id=self.other_org.id,
            job_type="sam_ingest",
            status="success",
            started_at=dt.datetime(2026, 7, 13, 1, 0),
        )

        _html, context = self._render_list({
            "organization_id": str(self.org.id),
            "job_type": "daily_snapshot",
            "status": "partial_success",
            "date_from": "2026-07-14",
            "date_to": "2026-07-14",
        })

        self.assertEqual([row["run"].id for row in context["runs"]], [matching.id])
        self.assertEqual(context["total_count"], 1)

    def test_detail_renders_metrics_and_safe_error_information(self):
        run = self._run(
            status="paused",
            error_type="RuntimeError",
            error_message="API key was redacted before storage",
            details_json={
                "source_configs_processed": 2,
                "records_seen": 49,
                "created": 3,
                "filtered": 46,
                "checkpoint_saved": True,
                "pause_reason": "rate_limit",
                "api_key": "should-not-render",
            },
        )

        html, context = self._render_detail(run.id)

        self.assertEqual(context["row"]["status_label"], "Paused")
        self.assertIn("Source configs processed", html)
        self.assertIn("Records seen", html)
        self.assertIn("Checkpoint saved", html)
        self.assertIn("Yes", html)
        self.assertIn("Rate Limit", html)
        self.assertIn("RuntimeError", html)
        self.assertIn("API key was redacted before storage", html)
        self.assertNotIn("should-not-render", html)

    def test_empty_and_no_match_states_render(self):
        empty_html, _context = self._render_list()
        self.assertIn("No operational runs have been recorded yet.", empty_html)

        self._run(status="success")
        no_match_html, _context = self._render_list({"status": "failed"})
        self.assertIn("No job runs match the selected filters.", no_match_html)

    def test_pagination_limits_to_page_size(self):
        for index in range(30):
            self._run(
                started_at=dt.datetime(2026, 7, 14, 1, 0) + dt.timedelta(minutes=index),
                summary=f"Run {index}",
            )

        html, context = self._render_list()

        self.assertEqual(len(context["runs"]), platform.OPERATIONS_PAGE_SIZE)
        self.assertEqual(context["total_count"], 30)
        self.assertEqual(context["total_pages"], 2)
        self.assertIn("Next", html)


if __name__ == "__main__":
    unittest.main()
