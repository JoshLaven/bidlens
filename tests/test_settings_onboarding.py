import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import Event, Organization, OrganizationMembership, User
from bidlens.routes.settings import settings_save


class FeedRulesOnboardingTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Settings Org", slug="settings-org")
        self.db.add(self.org)
        self.db.flush()
        self.admin = User(email="admin@settings.test", organization_id=self.org.id)
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
        setattr(self.admin, "current_organization_is_live", False)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _request(self):
        return SimpleNamespace(url=SimpleNamespace(query=f"org_id={self.org.id}"))

    def test_pre_live_feed_rules_save_records_completion_and_returns_to_setup(self):
        with patch("bidlens.routes.settings.require_user", return_value=self.admin):
            response = asyncio.run(settings_save(
                self._request(),
                include_keywords="research",
                exclude_keywords="construction",
                include_agencies="HHS",
                exclude_agencies="",
                min_days_out="5",
                max_days_out="90",
                digest_max_items="20",
                digest_recipients="",
                digest_time_local="07:00",
                triage_enabled="1",
                db=self.db,
            ))

        event = (
            self.db.query(Event)
            .filter(Event.org_id == self.org.id, Event.event_type == "feed_rules_configured")
            .one()
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/organization-setup?org_id={self.org.id}")
        self.assertEqual(event.payload["source"], "settings")

    def test_live_feed_rules_save_preserves_settings_redirect(self):
        self.org.is_live = True
        self.db.commit()

        with patch("bidlens.routes.settings.require_user", return_value=self.admin):
            response = asyncio.run(settings_save(
                self._request(),
                include_keywords="research",
                exclude_keywords="",
                include_agencies="",
                exclude_agencies="",
                min_days_out="",
                max_days_out="",
                digest_max_items="20",
                digest_recipients="",
                digest_time_local="07:00",
                triage_enabled=None,
                db=self.db,
            ))

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/settings?org_id={self.org.id}")


if __name__ == "__main__":
    unittest.main()
