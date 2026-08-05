import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import Organization, OrganizationMembership, PursuitLane, User
from bidlens.routes import settings


class MySettingsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Waystar Royco", slug="waystar-settings")
        self.db.add(self.org)
        self.db.flush()
        self.user = User(
            name="Kendall Roy",
            email="kendall@example.com",
            organization_id=self.org.id,
        )
        self.db.add(self.user)
        self.db.flush()
        self.db.add(OrganizationMembership(
            organization_id=self.org.id,
            user_id=self.user.id,
            role="admin",
        ))
        self.lane = PursuitLane(
            organization_id=self.org.id,
            name="Health",
            description="Health opportunities",
        )
        self.db.add(self.lane)
        self.db.commit()
        self.user.current_organization_id = self.org.id
        self.user.current_role = "admin"

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def _request(query=""):
        return SimpleNamespace(
            url=SimpleNamespace(query=query),
            query_params={key: value for key, value in [part.split("=", 1) for part in query.split("&") if "=" in part]},
            headers={},
        )

    def test_page_supplies_account_daily_brief_and_lane_context(self):
        with (
            patch.object(settings, "require_user", return_value=self.user),
            patch.object(settings.templates, "TemplateResponse", return_value={"ok": True}) as response,
        ):
            result = asyncio.run(settings.my_settings_page(self._request(), self.db))

        self.assertEqual(result, {"ok": True})
        context = response.call_args.args[1]
        self.assertEqual(context["organization"].name, "Waystar Royco")
        self.assertEqual(context["role"], "admin")
        self.assertEqual([lane.name for lane in context["lanes"]], ["Health"])
        self.assertEqual(context["my_lane_ids"], set())

    def test_daily_brief_preference_updates_existing_user_field(self):
        with patch.object(settings, "require_user", return_value=self.user):
            disabled = asyncio.run(settings.save_daily_brief_settings(
                self._request("org_id=1"), daily_brief_email_enabled=None, db=self.db
            ))
            self.assertTrue(self.user.daily_brief_email_opted_out)
            self.assertEqual(disabled.headers["location"], "/my-settings?org_id=1&saved=daily-brief")

            asyncio.run(settings.save_daily_brief_settings(
                self._request(), daily_brief_email_enabled="1", db=self.db
            ))
            self.assertFalse(self.user.daily_brief_email_opted_out)

    def test_template_is_consolidated_and_contains_no_launcher_cards_or_placeholders(self):
        template = Path("src/bidlens/templates/my_settings.html").read_text()
        for heading in ("Account", "Daily Brief", "My Lanes"):
            self.assertIn(f">{heading}<", template)
        self.assertNotIn("personal-settings-card", template)
        self.assertNotIn("Notifications", template)
        self.assertNotIn("Future personal notifications", template)
        self.assertIn("daily_brief_email_opted_out", template)
        self.assertIn("Select the lanes that personalize your Daily Brief.", template)
        self.assertIn('name="lane_ids"', template)
        self.assertIn('class="bidlens-switch"', template)
        self.assertIn('class="bidlens-switch-knob"', template)
        self.assertIn(
            'href="/company-profile?org_id={{ organization.id }}" class="btn btn-outline-secondary"',
            template,
        )
        self.assertIn("View Organization Profile", template)

        styles = Path("src/bidlens/static/css/styles.css").read_text()
        self.assertIn(".personal-settings-preference-form .bidlens-switch-field", styles)
        self.assertIn(".my-lanes-settings-list", styles)
        self.assertGreaterEqual(styles.count("width: min(100%, 540px);"), 2)

    def test_lane_save_returns_to_consolidated_page(self):
        with patch.object(settings, "require_user", return_value=self.user):
            response = asyncio.run(settings.save_my_lanes_settings(
                self._request(), lane_ids=[self.lane.id], db=self.db
            ))
        self.assertEqual(response.headers["location"], "/my-settings?saved=lanes")


if __name__ == "__main__":
    unittest.main()
