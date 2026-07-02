import datetime as dt
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.ingest_grants_gov import upsert_grants_gov_opportunity
from bidlens.ingest_sam import upsert_opportunity
from bidlens.models import Opportunity, OpportunityUpdateEvent, Organization
from bidlens.services.govwin_import import upsert_govwin_opportunity
from bidlens.services.opportunity_monitor import apply_source_update


class OpportunityMonitorTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Monitor Test", slug="monitor-test")
        self.db.add(self.org)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _opportunity(self, **overrides):
        values = {
            "organization_id": self.org.id,
            "source": "sam",
            "source_record_id": "notice-1",
            "title": "Original title",
            "agency": "Original agency",
            "opportunity_type": "Solicitation",
            "posted_date": dt.date(2026, 6, 1),
            "response_deadline": dt.date(2026, 7, 1),
            "description": "Original description",
            "description_text": "Original description",
            "raw_source_payload": {"revision": 1},
            "upserted_at": dt.datetime(2026, 6, 1),
            "last_seen_at": dt.datetime(2026, 6, 1),
        }
        values.update(overrides)
        opportunity = Opportunity(**values)
        self.db.add(opportunity)
        self.db.flush()
        return opportunity

    def test_unchanged_observation_updates_only_last_seen(self):
        opportunity = self._opportunity()
        previous_upserted_at = opportunity.upserted_at
        previous_updated_at = opportunity.updated_at
        observed_at = dt.datetime(2026, 6, 30, 12, 0)

        result = apply_source_update(
            self.db,
            opportunity,
            {
                "title": opportunity.title,
                "raw_source_payload": {"revision": 2},
            },
            observed_at=observed_at,
        )
        self.db.flush()

        self.assertFalse(result.changed)
        self.assertEqual(opportunity.last_seen_at, observed_at)
        self.assertEqual(opportunity.upserted_at, previous_upserted_at)
        self.assertEqual(opportunity.updated_at, previous_updated_at)
        self.assertEqual(opportunity.raw_source_payload, {"revision": 1})
        self.assertEqual(self.db.query(OpportunityUpdateEvent).count(), 0)

    def test_unlinked_change_is_recorded_without_salesforce_sync(self):
        opportunity = self._opportunity()

        result = apply_source_update(
            self.db,
            opportunity,
            {"title": "Changed title", "raw_source_payload": {"revision": 2}},
        )
        self.db.flush()

        self.assertTrue(result.changed)
        self.assertEqual(opportunity.title, "Changed title")
        self.assertEqual(opportunity.raw_source_payload, {"revision": 2})
        event = self.db.query(OpportunityUpdateEvent).one()
        self.assertEqual(event.salesforce_sync_status, "not_linked")
        self.assertIsNone(event.salesforce_payload)
        self.assertEqual(result.update_event_id, event.id)

    @patch("bidlens.services.opportunity_monitor.SalesforceService")
    def test_linked_change_records_successful_salesforce_sync(self, service_class):
        opportunity = self._opportunity(salesforce_opportunity_id="006TEST")

        result = apply_source_update(
            self.db,
            opportunity,
            {
                "title": "Changed title",
                "response_deadline": dt.date(2026, 7, 15),
            },
            observed_at=dt.datetime(2026, 6, 30, 12, 0),
        )
        self.db.flush()
        event = self.db.query(OpportunityUpdateEvent).one()

        self.assertEqual(result.salesforce_sync_status, "succeeded")
        self.assertEqual(event.salesforce_sync_status, "succeeded")
        self.assertEqual(set(event.changed_fields), {"title", "response_deadline"})
        self.assertEqual(
            event.salesforce_payload,
            {"Name": "Changed title", "CloseDate": "2026-07-15"},
        )
        service_class.return_value.update_opportunity.assert_called_once_with(
            "006TEST",
            {"Name": "Changed title", "CloseDate": "2026-07-15"},
        )
        self.assertIsNotNone(opportunity.salesforce_synced_at)

    @patch("bidlens.services.opportunity_monitor.SalesforceService")
    def test_linked_change_is_retained_when_salesforce_sync_fails(self, service_class):
        service_class.return_value.update_opportunity.side_effect = RuntimeError(
            "Salesforce unavailable"
        )
        opportunity = self._opportunity(salesforce_opportunity_id="006TEST")

        result = apply_source_update(
            self.db,
            opportunity,
            {"description_text": "Revised description"},
        )
        self.db.flush()
        event = self.db.query(OpportunityUpdateEvent).one()

        self.assertTrue(result.changed)
        self.assertEqual(opportunity.description_text, "Revised description")
        self.assertEqual(event.salesforce_sync_status, "failed")
        self.assertIn("Salesforce unavailable", event.salesforce_error)
        self.assertIsNone(opportunity.salesforce_synced_at)

    @patch("bidlens.services.opportunity_monitor.SalesforceService")
    def test_all_source_upserts_use_the_monitor(self, service_class):
        cases = (
            (
                "sam",
                "sam-1",
                lambda data: upsert_opportunity(self.db, self.org.id, data),
                "updated",
            ),
            (
                "grants_gov",
                "grant-1",
                lambda data: upsert_grants_gov_opportunity(self.db, self.org.id, data),
                "updated",
            ),
            (
                "govwin_export",
                "govwin-1",
                lambda data: upsert_govwin_opportunity(self.db, self.org.id, data)[0],
                "updated",
            ),
        )

        for source, source_record_id, upsert, expected_status in cases:
            with self.subTest(source=source):
                opportunity = self._opportunity(
                    source=source,
                    source_record_id=source_record_id,
                    salesforce_opportunity_id=f"006-{source}",
                )
                status = upsert(
                    {
                        "source": source,
                        "source_record_id": source_record_id,
                        "title": f"Updated {source} title",
                    }
                )
                self.assertEqual(status, expected_status)
                self.assertEqual(opportunity.title, f"Updated {source} title")

        self.db.flush()
        self.assertEqual(self.db.query(OpportunityUpdateEvent).count(), 3)
        self.assertEqual(service_class.return_value.update_opportunity.call_count, 3)


if __name__ == "__main__":
    unittest.main()
