import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import Event, Organization, OrganizationMembership, SamSourceConfig, User
from bidlens.routes.connect_sources import _source_context, enable_grants_source, save_connect_sam


class ConnectSourcesTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Connect Org", slug="connect-org")
        self.db.add(self.org)
        self.db.flush()
        self.admin = User(email="admin@connect.test", organization_id=self.org.id)
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

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_sam_setup_creates_source_and_returns_to_connect_sources(self):
        request = SimpleNamespace(query_params={})

        with patch("bidlens.routes.connect_sources.require_admin", return_value=self.admin):
            response = asyncio.run(save_connect_sam(
                request,
                search_name="Primary SAM.gov Search",
                naics_codes="541611\n541990",
                keywords="analytics",
                notice_types=["Solicitation", "Sources Sought"],
                posted_days_back="30",
                max_records="100",
                db=self.db,
            ))

        config = self.db.query(SamSourceConfig).filter(SamSourceConfig.organization_id == self.org.id).one()
        event = (
            self.db.query(Event)
            .filter(
                Event.org_id == self.org.id,
                Event.event_type == "opportunity_sources_connected",
            )
            .one()
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/connect-sources?org_id={self.org.id}&saved=sam")
        self.assertEqual(config.naics_codes, ["541611", "541990"])
        self.assertEqual(config.notice_types, ["Solicitation", "Sources Sought"])
        self.assertEqual(event.payload["source"], "sam.gov")

    def test_grants_enable_is_one_click_and_marks_source_connected(self):
        request = SimpleNamespace(query_params={})

        with patch("bidlens.routes.connect_sources.require_admin", return_value=self.admin):
            response = asyncio.run(enable_grants_source(request, db=self.db))

        events = (
            self.db.query(Event)
            .filter(
                Event.org_id == self.org.id,
                Event.event_type == "opportunity_source_enabled",
            )
            .all()
        )
        context = _source_context(self.db, self.org.id)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/connect-sources?org_id={self.org.id}&saved=grants")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].payload["source"], "grants.gov")
        self.assertEqual(events[0].payload["configuration_flow"], "one_click_enable")
        self.assertTrue(context["grants"]["connected"])

    def test_grants_enable_is_idempotent(self):
        request = SimpleNamespace(query_params={})

        with patch("bidlens.routes.connect_sources.require_admin", return_value=self.admin):
            asyncio.run(enable_grants_source(request, db=self.db))
            asyncio.run(enable_grants_source(request, db=self.db))

        count = (
            self.db.query(Event)
            .filter(
                Event.org_id == self.org.id,
                Event.event_type == "opportunity_source_enabled",
            )
            .count()
        )
        self.assertEqual(count, 1)

    def test_discovery_and_outbound_templates_stay_separated(self):
        template_dir = Path("src/bidlens/templates")
        discovery = (template_dir / "connect_sources.html").read_text()
        outbound = (template_dir / "outbound_integrations.html").read_text()

        self.assertIn("Opportunity Discovery", discovery)
        self.assertNotIn("Salesforce", discovery)
        self.assertNotIn("Outbound Integrations", discovery)
        self.assertIn("Outbound Integrations", outbound)
        self.assertNotIn("SAM.gov", outbound)
        self.assertNotIn("Grants.gov", outbound)
        self.assertNotIn("GovWin", outbound)


if __name__ == "__main__":
    unittest.main()
