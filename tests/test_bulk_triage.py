import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import Opportunity, Organization
from bidlens.routes import api


class BulkTriageTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Bulk Triage", slug="bulk-triage")
        self.db.add(self.org)
        self.db.flush()
        self.user = SimpleNamespace(id=1, organization_id=self.org.id)
        self.opportunities = [
            Opportunity(
                organization_id=self.org.id,
                source="sam",
                source_record_id=f"triage-{index}",
                title=f"Triage opportunity {index}",
                agency="Test Agency",
                opportunity_type="Solicitation",
                posted_date=date(2026, 6, 1),
                response_deadline=date(2026, 7, 15),
                qualification_status="unreviewed",
            )
            for index in range(2)
        ]
        self.db.add_all(self.opportunities)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _bulk_action(self, opp_ids, action):
        with patch.object(api, "require_admin", return_value=self.user):
            return api.bulk_set_qualification_status(
                api.BulkQualificationIn(opp_ids=opp_ids, action=action),
                MagicMock(),
                self.db,
            )

    def test_qualify_selected_updates_only_selected_opportunities(self):
        selected = self.opportunities[0]

        result = self._bulk_action([selected.id], "qualify")
        self.db.refresh(selected)
        self.db.refresh(self.opportunities[1])

        self.assertEqual(result["updated_count"], 1)
        self.assertEqual(selected.qualification_status, "qualified")
        self.assertEqual(self.opportunities[1].qualification_status, "unreviewed")

    def test_reject_selected_marks_records_rejected_without_deleting(self):
        opp_ids = [opportunity.id for opportunity in self.opportunities]

        result = self._bulk_action(opp_ids, "reject")

        self.assertEqual(result["updated_count"], 2)
        self.assertEqual(
            self.db.query(Opportunity).filter(Opportunity.id.in_(opp_ids)).count(),
            2,
        )
        self.assertEqual(
            {
                opportunity.qualification_status
                for opportunity in self.db.query(Opportunity).filter(Opportunity.id.in_(opp_ids))
            },
            {"rejected"},
        )

    def test_bulk_action_rejects_records_no_longer_awaiting_triage(self):
        opportunity = self.opportunities[0]
        opportunity.qualification_status = "qualified"
        self.db.commit()

        with self.assertRaises(HTTPException) as raised:
            self._bulk_action([opportunity.id], "reject")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("no longer awaiting triage", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
