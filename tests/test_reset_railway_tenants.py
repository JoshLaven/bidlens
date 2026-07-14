import datetime as dt
import importlib.util
import sys
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import Event, Opportunity, Organization, OrganizationMembership, User, Workspace


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "reset_railway_tenants.py"
SPEC = importlib.util.spec_from_file_location("reset_railway_tenants", SCRIPT_PATH)
reset_railway_tenants = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reset_railway_tenants
SPEC.loader.exec_module(reset_railway_tenants)


class RailwayTenantResetTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

        self.platform_org = Organization(
            name="BidLens Platform",
            slug="bidlens-platform",
            plan="platform",
            is_live=True,
        )
        self.tenant_org = Organization(
            name="The Office",
            slug="the-office",
            email_domain="theoffice.com",
        )
        self.db.add_all([self.platform_org, self.tenant_org])
        self.db.flush()

        self.platform_user = User(
            email="joshuatlaven@gmail.com",
            organization_id=self.platform_org.id,
        )
        self.tenant_user = User(
            email="jimh@theoffice.com",
            organization_id=self.tenant_org.id,
        )
        self.db.add_all([self.platform_user, self.tenant_user])
        self.db.flush()
        self.db.add_all([
            OrganizationMembership(
                organization_id=self.platform_org.id,
                user_id=self.platform_user.id,
                role="admin",
            ),
            OrganizationMembership(
                organization_id=self.tenant_org.id,
                user_id=self.tenant_user.id,
                role="admin",
            ),
            Workspace(
                organization_id=self.tenant_org.id,
                name="The Office Workspace",
                slug="the-office",
            ),
        ])
        self.opportunity = Opportunity(
            organization_id=self.tenant_org.id,
            source="sam.gov",
            source_record_id="notice-1",
            title="Tenant Opportunity",
            agency="GSA",
            opportunity_type="RFP",
            posted_date=dt.date(2026, 7, 1),
            response_deadline=dt.date(2026, 8, 1),
        )
        self.db.add(self.opportunity)
        self.db.flush()
        self.db.add_all([
            Event(org_id=self.tenant_org.id, event_type="tenant_org_event"),
            Event(user_id=self.tenant_user.id, event_type="tenant_user_event"),
            Event(opp_id=self.opportunity.id, event_type="tenant_opp_event"),
            Event(org_id=self.platform_org.id, event_type="platform_org_event"),
            Event(user_id=self.platform_user.id, event_type="platform_user_event"),
            Event(org_id=None, user_id=None, opp_id=None, event_type="global_event"),
        ])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_event_delete_plan_preserves_platform_and_global_events(self):
        scope = reset_railway_tenants.build_scope(self.db)
        plan = reset_railway_tenants.build_delete_plan(self.db, scope)
        events_item = next(item for item in plan if item.label == "events")
        summary = reset_railway_tenants.event_summary(self.db, scope)

        self.assertEqual(events_item.count, 1)
        self.assertEqual(summary["tenant_events_to_delete"], 1)
        self.assertEqual(summary["platform_global_events_to_keep"], 5)
        self.assertEqual(summary["other_events_to_keep"], 0)

        events_item.delete(True)
        self.db.commit()

        remaining_event_types = {
            event_type
            for (event_type,) in self.db.query(Event.event_type).order_by(Event.event_type).all()
        }
        self.assertEqual(
            remaining_event_types,
            {
                "global_event",
                "platform_org_event",
                "platform_user_event",
                "tenant_opp_event",
                "tenant_user_event",
            },
        )


if __name__ == "__main__":
    unittest.main()
