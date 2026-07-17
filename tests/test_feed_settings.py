import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import Organization, OrganizationMembership, OrgProfile, PursuitLane, User
from bidlens.routes import pursuit_lanes, settings


class FeedSettingsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Feed Settings Org", slug="feed-settings-org", is_live=True)
        self.other_org = Organization(name="Other Feed Org", slug="other-feed-org")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.admin = User(email="admin@feed.test", organization_id=self.org.id)
        self.db.add(self.admin)
        self.db.flush()
        self.db.add(OrganizationMembership(
            organization_id=self.org.id,
            user_id=self.admin.id,
            role="admin",
        ))
        self.db.commit()
        setattr(self.admin, "current_organization_id", self.org.id)
        setattr(self.admin, "current_role", "admin")
        setattr(self.admin, "current_organization_is_live", True)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _request(self, path="/settings"):
        return SimpleNamespace(
            query_params={"org_id": str(self.org.id)},
            url=SimpleNamespace(path=path, query=f"org_id={self.org.id}"),
        )

    def test_settings_page_contains_workflow_and_tenant_scoped_lane_management(self):
        self.db.add_all([
            PursuitLane(organization_id=self.org.id, name="Healthcare", agencies=[] , naics=[], keywords=[], set_asides=[]),
            PursuitLane(organization_id=self.other_org.id, name="Secret Other Lane", agencies=[], naics=[], keywords=[], set_asides=[]),
        ])
        self.db.commit()
        with (
            patch.object(settings, "require_user", return_value=self.admin),
            patch.object(settings.templates, "TemplateResponse", return_value={"ok": True}) as response,
        ):
            result = asyncio.run(settings.settings_page(self._request(), self.db))

        self.assertEqual(result, {"ok": True})
        self.assertEqual(response.call_args.args[0], "pursuit_lanes.html")
        context = response.call_args.args[1]
        self.assertEqual([lane.name for lane in context["lanes"]], ["Healthcare"])
        self.assertTrue(context["is_admin"])

    def test_feed_settings_template_retains_live_filters_and_hides_digest_controls(self):
        template = Path("src/bidlens/templates/pursuit_lanes.html").read_text()
        self.assertIn("Feed Settings", template)
        self.assertIn("Require triage before Feed", template)
        self.assertIn("Pursuit Lanes", template)
        for field in ("include_keywords", "exclude_keywords", "include_agencies", "exclude_agencies", "min_days_out", "max_days_out"):
            self.assertIn(f'name="{field}"', template)
        self.assertNotIn('name="digest_recipients"', template)
        self.assertNotIn('name="digest_max_items"', template)
        self.assertNotIn('name="digest_time_local"', template)

    def test_triage_save_preserves_hidden_digest_values(self):
        profile = OrgProfile(
            org_id=self.org.id,
            digest_recipients="legacy@example.com",
            digest_max_items=42,
            digest_time_local="06:30",
        )
        self.db.add(profile)
        self.db.commit()
        with patch.object(settings, "require_user", return_value=self.admin):
            response = asyncio.run(settings.settings_save(
                self._request(),
                include_keywords="research",
                exclude_keywords="construction",
                include_agencies="HHS",
                exclude_agencies="DoD",
                min_days_out="3",
                max_days_out="60",
                digest_max_items=None,
                digest_recipients=None,
                digest_time_local=None,
                triage_enabled="1",
                db=self.db,
            ))
        self.db.refresh(profile)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(profile.triage_enabled)
        self.assertEqual(profile.digest_recipients, "legacy@example.com")
        self.assertEqual(profile.digest_max_items, 42)
        self.assertEqual(profile.digest_time_local, "06:30")

    def test_legacy_pursuit_lanes_route_redirects_to_feed_settings(self):
        with patch.object(pursuit_lanes, "require_user", return_value=self.admin):
            response = asyncio.run(pursuit_lanes.pursuit_lanes_page(
                self._request("/pursuit-lanes"), self.db
            ))
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/settings?org_id={self.org.id}")

    def test_lane_creation_remains_workspace_scoped_and_returns_to_feed_settings(self):
        with patch.object(pursuit_lanes, "require_user", return_value=self.admin):
            response = asyncio.run(pursuit_lanes.create_pursuit_lane(
                self._request("/pursuit-lanes"),
                name="Federal Health",
                description="Health work",
                agencies="HHS",
                naics="541611",
                keywords="health",
                set_asides="Small Business",
                is_active="1",
                db=self.db,
            ))
        lane = self.db.query(PursuitLane).one()
        self.assertEqual(lane.organization_id, self.org.id)
        self.assertEqual(response.headers["location"], f"/settings?org_id={self.org.id}")


if __name__ == "__main__":
    unittest.main()
