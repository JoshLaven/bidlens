import datetime as dt
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import Event, Opportunity, Organization, User, Vote
from bidlens.routes import imports
from bidlens.services.market_activity import (
    MarketActivityFilters,
    build_market_activity,
    market_activity_filter_options,
)


class MarketActivityAggregationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Market Org", slug="market-org")
        self.other_org = Organization(name="Other Org", slug="other-org")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.user = User(email="admin@market.test", organization_id=self.org.id)
        self.db.add(self.user)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _opportunity(self, record_id, created_at, **overrides):
        values = {
            "organization_id": self.org.id,
            "source": "sam.gov",
            "source_record_id": record_id,
            "title": f"Opportunity {record_id}",
            "agency": "Agency A",
            "opportunity_type": "Solicitation",
            "posted_date": created_at.date(),
            "response_deadline": dt.date(2026, 5, 1),
            "qualification_status": "unreviewed",
            "decision_state": "INBOX",
            "created_at": created_at,
        }
        values.update(overrides)
        opportunity = Opportunity(**values)
        self.db.add(opportunity)
        self.db.flush()
        return opportunity

    def _filters(self, **overrides):
        values = {
            "start_date": dt.date(2026, 1, 1),
            "end_date": dt.date(2026, 3, 31),
        }
        values.update(overrides)
        return MarketActivityFilters(**values)

    def _seed_activity(self):
        qualified_sf = self._opportunity(
            "one",
            dt.datetime(2026, 1, 5),
            qualification_status="qualified",
            naics="541611",
            account_type="Federal",
            salesforce_opportunity_id="006-ONE",
        )
        pursued = self._opportunity(
            "two",
            dt.datetime(2026, 1, 20),
            source="grants.gov",
            opportunity_type="Grant",
            naics=None,
            account_type="State Government",
            response_deadline=dt.date(2026, 4, 15),
        )
        rejected = self._opportunity(
            "three",
            dt.datetime(2026, 2, 3),
            agency="Agency B",
            naics="541690",
            account_type="Regional Government",
            qualification_status="rejected",
        )
        qualified_passed = self._opportunity(
            "four",
            dt.datetime(2026, 2, 22),
            source="govwin_export",
            agency="Agency C",
            opportunity_type="IT Services",
            account_type="Nonprofit University",
            qualification_status="qualified",
        )
        archived = self._opportunity(
            "five",
            dt.datetime(2026, 3, 2),
            qualification_status="qualified",
            decision_state="ARCHIVED",
            response_deadline=dt.date(2026, 2, 28),
        )
        self._opportunity(
            "outside-range",
            dt.datetime(2025, 12, 15),
            qualification_status="qualified",
        )
        self._opportunity(
            "other-tenant",
            dt.datetime(2026, 1, 10),
            organization_id=self.other_org.id,
            qualification_status="qualified",
            agency="Other Tenant Agency",
            naics="999999",
        )
        self.db.add_all([
            Vote(org_id=self.org.id, opp_id=pursued.id, user_id=self.user.id, vote="PURSUE"),
            Vote(org_id=self.org.id, opp_id=qualified_passed.id, user_id=self.user.id, vote="PASS"),
            Event(
                org_id=self.org.id,
                user_id=self.user.id,
                opp_id=pursued.id,
                event_type="crm_pushed",
                ui_version="v1",
                payload={"crm_pushed": True},
            ),
        ])
        self.db.commit()
        return {
            "qualified_sf": qualified_sf,
            "pursued": pursued,
            "rejected": rejected,
            "qualified_passed": qualified_passed,
            "archived": archived,
        }

    def test_metrics_and_monthly_aggregations_respect_workspace(self):
        self._seed_activity()

        result = build_market_activity(
            self.db,
            organization_id=self.org.id,
            filters=self._filters(),
            today=dt.date(2026, 3, 1),
        )

        self.assertEqual(
            result["metrics"],
            {
                "total": 5,
                "qualified": 4,
                "rejected": 3,
                "pushed": 2,
                "active_open": 2,
            },
        )
        self.assertEqual(
            [(row["key"], row["imported"], row["qualified"]) for row in result["monthly"]],
            [("2026-01", 2, 2), ("2026-02", 2, 1), ("2026-03", 1, 1)],
        )
        self.assertEqual(result["by_source"][0], {"label": "sam.gov", "count": 3})
        self.assertEqual(result["top_agencies"][0], {"label": "Agency A", "count": 3})
        self.assertEqual(
            {row["label"] for row in result["by_account_type"]},
            {
                "Federal",
                "State Government",
                "Regional Government",
                "Nonprofit University",
                "__other__",
            },
        )
        self.assertNotIn(
            "Grant",
            {row["label"] for row in result["top_categories"]},
        )
        self.assertEqual(
            result["upcoming_due_dates"],
            [
                {"key": "2026-04", "label": "Apr 2026", "count": 1},
                {"key": "2026-05", "label": "May 2026", "count": 1},
            ],
        )

    def test_source_and_workflow_filters_apply_to_all_aggregations(self):
        self._seed_activity()

        sam = build_market_activity(
            self.db,
            organization_id=self.org.id,
            filters=self._filters(source="sam.gov"),
            today=dt.date(2026, 3, 1),
        )
        qualified_only = build_market_activity(
            self.db,
            organization_id=self.org.id,
            filters=self._filters(qualified_only=True),
            today=dt.date(2026, 3, 1),
        )
        pushed_only = build_market_activity(
            self.db,
            organization_id=self.org.id,
            filters=self._filters(pushed_only=True),
            today=dt.date(2026, 3, 1),
        )
        state_government = build_market_activity(
            self.db,
            organization_id=self.org.id,
            filters=self._filters(account_type="State Government"),
            today=dt.date(2026, 3, 1),
        )

        self.assertEqual(sam["metrics"]["total"], 3)
        self.assertEqual(sam["metrics"]["qualified"], 2)
        self.assertEqual(sam["metrics"]["rejected"], 2)
        self.assertEqual(qualified_only["metrics"]["total"], 4)
        self.assertEqual(pushed_only["metrics"]["total"], 2)
        self.assertEqual(state_government["metrics"]["total"], 1)
        self.assertEqual(
            {row["label"] for row in pushed_only["by_source"]},
            {"sam.gov", "grants.gov"},
        )

    def test_account_type_naics_and_filter_options_are_tenant_scoped(self):
        self._seed_activity()

        filtered = build_market_activity(
            self.db,
            organization_id=self.org.id,
            filters=self._filters(account_type="Regional Government", category="541690"),
            today=dt.date(2026, 3, 1),
        )
        options = market_activity_filter_options(
            self.db,
            organization_id=self.org.id,
        )

        self.assertEqual(filtered["metrics"]["total"], 1)
        self.assertEqual(filtered["metrics"]["rejected"], 1)
        self.assertIn("541611", options["categories"])
        self.assertNotIn("Grant", options["categories"])
        self.assertNotIn("Solicitation", options["categories"])
        self.assertIn("Regional Government", options["account_types"])
        self.assertNotIn("999999", options["categories"])


class MarketActivityRouteAccessTests(unittest.TestCase):
    def test_member_cannot_access_analytics_page(self):
        member = SimpleNamespace(
            id=1,
            organization_id=7,
            current_organization_id=7,
            current_role="member",
        )
        request = SimpleNamespace(query_params={}, url=SimpleNamespace(query=""))

        with (
            patch("bidlens.routes.imports.get_current_user", return_value=member),
            patch("bidlens.routes.imports.attach_request_user_context", return_value=member),
        ):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(imports.market_activity_page(request, db=MagicMock()))

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.detail, "Only Workspace Admins can view Analytics.")


if __name__ == "__main__":
    unittest.main()
