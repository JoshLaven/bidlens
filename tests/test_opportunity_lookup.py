import asyncio
import datetime as dt
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from bidlens.database import Base
from bidlens.models import Opportunity, Organization, User, Vote
from bidlens.routes import imports


class OpportunityLookupTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Lookup Org", slug="lookup-org")
        self.other_org = Organization(name="Other Lookup Org", slug="other-lookup-org")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.admin = User(email="lookup-admin@example.com", organization_id=self.org.id)
        self.db.add(self.admin)
        self.db.flush()
        self.admin.current_organization_id = self.org.id
        self.admin.current_role = "admin"

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def _request(query_string: bytes = b""):
        return Request({
            "type": "http",
            "method": "GET",
            "path": "/admin/opportunity-lookup",
            "query_string": query_string,
            "headers": [],
        })

    def _opportunity(self, *, organization_id=None, **overrides):
        values = {
            "organization_id": organization_id or self.org.id,
            "source": "sam",
            "source_record_id": "source-default",
            "solicitation_number": "SOL-DEFAULT",
            "sam_notice_id": "sam-default",
            "govwin_staging_id": None,
            "title": "Default opportunity",
            "agency": "Default Agency",
            "opportunity_type": "Solicitation",
            "posted_date": dt.date(2026, 7, 1),
            "response_deadline": dt.date(2026, 8, 1),
            "qualification_status": "qualified",
        }
        values.update(overrides)
        opportunity = Opportunity(**values)
        self.db.add(opportunity)
        self.db.flush()
        return opportunity

    def _lookup(self, query: str):
        with patch("bidlens.routes.imports.require_admin", return_value=self.admin):
            return asyncio.run(imports.opportunity_lookup_page(
                request=self._request(f"q={query}".encode()),
                q=query,
                page=1,
                db=self.db,
            ))

    def test_lookup_searches_identifying_fields_and_respects_tenancy(self):
        expected = self._opportunity(
            source_record_id="GRANT-LOOKUP-42",
            solicitation_number="SOL-LOOKUP-42",
            sam_notice_id="SAM-LOOKUP-42",
            govwin_staging_id="GW-LOOKUP-42",
            title="Behavioral Health Lookup",
            agency="Lookup Services Agency",
        )
        self._opportunity(
            organization_id=self.other_org.id,
            source_record_id="GRANT-LOOKUP-42",
            title="Other tenant copy",
        )
        self.db.commit()

        for query in (
            "Behavioral Health",
            "Lookup Services",
            "SOL-LOOKUP-42",
            "GRANT-LOOKUP-42",
            "SAM-LOOKUP-42",
            "GW-LOOKUP-42",
        ):
            with self.subTest(query=query):
                response = self._lookup(query)
                self.assertEqual(response.context["total_results"], 1)
                self.assertEqual(
                    response.context["results"][0]["opportunity"].id,
                    expected.id,
                )

    def test_lookup_separates_workflow_user_relationship_and_crm_state(self):
        opportunity = self._opportunity(
            source_record_id="shortlisted-record",
            title="Shortlisted lookup",
            salesforce_opportunity_id="006LOOKUP",
        )
        self.db.add(Vote(
            org_id=self.org.id,
            opp_id=opportunity.id,
            user_id=self.admin.id,
            vote="PURSUE",
        ))
        self.db.commit()

        response = self._lookup("shortlisted-record")
        item = response.context["results"][0]

        self.assertEqual(item["workflow_state"], "Qualified")
        self.assertEqual(item["user_relationship"], "Interested")
        self.assertEqual(item["crm_state"], "Salesforce linked")

    def test_organization_workflow_state_precedence(self):
        yesterday = dt.date.today() - dt.timedelta(days=1)
        today = dt.date.today()
        cases = (
            (
                self._opportunity(
                    source_record_id="archived-state",
                    decision_state="ARCHIVED",
                    qualification_status="unreviewed",
                    response_deadline=yesterday,
                ),
                "Archived",
            ),
            (
                self._opportunity(
                    source_record_id="rejected-state",
                    qualification_status="rejected",
                    response_deadline=yesterday,
                ),
                "Rejected",
            ),
            (
                self._opportunity(
                    source_record_id="qualified-state",
                    qualification_status="qualified",
                    response_deadline=yesterday,
                ),
                "Qualified",
            ),
            (
                self._opportunity(
                    source_record_id="expired-state",
                    qualification_status="unreviewed",
                    response_deadline=yesterday,
                ),
                "Expired",
            ),
            (
                self._opportunity(
                    source_record_id="pending-state",
                    qualification_status="unreviewed",
                    response_deadline=today,
                ),
                "Pending Review",
            ),
        )

        for opportunity, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    imports._opportunity_lookup_workflow_state(opportunity),
                    expected,
                )

    def test_empty_query_does_not_return_workspace_inventory(self):
        self._opportunity()
        self.db.commit()

        response = self._lookup("")

        self.assertEqual(response.context["total_results"], 0)
        self.assertEqual(response.context["results"], [])


if __name__ == "__main__":
    unittest.main()
