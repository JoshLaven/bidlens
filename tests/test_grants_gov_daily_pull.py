import unittest
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.grants_gov_client import search_recent_opportunities
from bidlens.ingest_grants_gov import ingest_grants_gov
from bidlens.models import Opportunity, OpportunityHistoryEvent, Organization
from bidlens.routes import opportunities
from bidlens.services.opportunity_history import (
    EVENT_GRANTS_FORECAST_VERSION,
    EVENT_GRANTS_SYNOPSIS_VERSION,
)


class GrantsGovDailyPullTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Grants Daily", slug="grants-daily")
        self.db.add(self.org)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def _record(record_id: str, title: str) -> dict:
        return {
            "id": record_id,
            "title": title,
            "agencyName": "Department of Health and Human Services",
            "openDate": "07/05/2026",
            "closeDate": "08/05/2026",
            "oppStatus": "posted",
        }

    @patch("bidlens.grants_gov_client._post_search")
    def test_search_uses_one_day_posted_date_window_and_record_offset(self, post_search):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"data": {"hitCount": 0, "oppHits": []}}
        post_search.return_value = response

        search_recent_opportunities(rows=25, start_record_num=50)

        payload = post_search.call_args.args[0]
        self.assertEqual(payload["dateRange"], "1")
        self.assertEqual(payload["startRecordNum"], 50)
        self.assertEqual(payload["rows"], 25)
        self.assertEqual(payload["oppStatuses"], "forecasted|posted")
        self.assertEqual(payload["keyword"], "")
        self.assertEqual(payload["agencies"], "")
        self.assertEqual(payload["eligibilities"], "")
        self.assertEqual(payload["fundingCategories"], "")
        self.assertEqual(payload["fundingInstruments"], "")

    @patch("bidlens.ingest_grants_gov.fetch_opportunity_detail", return_value={})
    @patch("bidlens.ingest_grants_gov.search_recent_opportunities")
    def test_daily_pull_paginates_to_hit_count(self, search, fetch_detail):
        search.side_effect = [
            {
                "data": {
                    "hitCount": 3,
                    "startRecord": 0,
                    "oppHits": [
                        self._record("grant-1", "First grant"),
                        self._record("grant-2", "Second grant"),
                    ],
                }
            },
            {
                "data": {
                    "hitCount": 3,
                    "startRecord": 2,
                    "oppHits": [self._record("grant-3", "Third grant")],
                }
            },
        ]

        result = ingest_grants_gov(
            self.db,
            organization_id=self.org.id,
            rows=2,
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["date_range_days"], 1)
        self.assertEqual(result["pages_pulled"], 2)
        self.assertEqual(result["received"], 3)
        self.assertEqual(result["created"], 3)
        self.assertEqual(
            [call.kwargs["start_record_num"] for call in search.call_args_list],
            [0, 2],
        )
        self.assertEqual(fetch_detail.call_count, 3)
        self.assertEqual(self.db.query(Opportunity).count(), 3)

    @patch("bidlens.ingest_grants_gov.fetch_opportunity_detail")
    @patch("bidlens.ingest_grants_gov.search_recent_opportunities")
    def test_zero_result_day_is_successful(self, search, fetch_detail):
        search.return_value = {
            "data": {
                "hitCount": 0,
                "startRecord": 0,
                "oppHits": [],
            }
        }

        result = ingest_grants_gov(self.db, organization_id=self.org.id)

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["received"], 0)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["pages_pulled"], 1)
        fetch_detail.assert_not_called()

    @patch("bidlens.ingest_grants_gov.fetch_opportunity_detail", return_value={})
    @patch("bidlens.ingest_grants_gov.search_recent_opportunities")
    def test_existing_grant_is_updated_without_duplicate(self, search, _fetch_detail):
        search.return_value = {
            "data": {
                "hitCount": 1,
                "oppHits": [self._record("grant-update", "Original title")],
            }
        }
        first = ingest_grants_gov(self.db, organization_id=self.org.id)
        self.assertEqual(first["created"], 1)

        search.return_value = {
            "data": {
                "hitCount": 1,
                "oppHits": [self._record("grant-update", "Revised title")],
            }
        }
        second = ingest_grants_gov(self.db, organization_id=self.org.id)

        self.assertEqual(second["created"], 0)
        self.assertEqual(second["updated"], 1)
        self.assertEqual(self.db.query(Opportunity).count(), 1)
        self.assertEqual(self.db.query(Opportunity).one().title, "Revised title")

    @patch("bidlens.ingest_grants_gov.fetch_opportunity_detail")
    @patch("bidlens.ingest_grants_gov.search_recent_opportunities")
    def test_detail_version_history_maps_to_deduplicated_history_events(self, search, fetch_detail):
        search.return_value = {
            "data": {
                "hitCount": 1,
                "oppHits": [self._record("grant-history", "Versioned grant")],
            }
        }
        fetch_detail.return_value = {
            "data": {
                "id": "grant-history",
                "title": "Versioned grant",
                "agencyName": "Department of Health and Human Services",
                "opportunityHistoryDetails": [
                    {
                        "revision": 1,
                        "synopsis": {
                            "version": 1,
                            "lastUpdatedDate": "Jun 24, 2026 01:31:14 PM EDT",
                        },
                    },
                    {
                        "revision": 2,
                        "synopsisModifiedFields": [
                            "revision",
                            "version",
                            "opportunityTitle",
                            "modComments",
                            "createTimeStamp",
                        ],
                        "synopsis": {
                            "version": 2,
                            "lastUpdatedDate": "Jun 24, 2026 01:33:27 PM EDT",
                            "modComments": "Revised funding opportunity description",
                        },
                    },
                    {
                        "revision": 3,
                        "forecast": {
                            "version": 1,
                            "lastUpdatedDate": "Jun 25, 2026 10:15:00 AM EDT",
                            "modComments": "Updated anticipated posting date",
                        },
                    },
                ],
            }
        }

        first = ingest_grants_gov(self.db, organization_id=self.org.id)
        second = ingest_grants_gov(self.db, organization_id=self.org.id)

        events = (
            self.db.query(OpportunityHistoryEvent)
            .filter(
                OpportunityHistoryEvent.event_type.in_(
                    (EVENT_GRANTS_SYNOPSIS_VERSION, EVENT_GRANTS_FORECAST_VERSION)
                )
            )
            .order_by(OpportunityHistoryEvent.occurred_at.asc())
            .all()
        )
        self.assertEqual(first["created"], 1)
        self.assertEqual(second["unchanged"], 1)
        self.assertEqual(len(events), 3)
        self.assertEqual(
            [event.event_data["version_name"] for event in events],
            ["Synopsis 1", "Synopsis 2", "Forecast 1"],
        )
        self.assertEqual(
            events[1].event_data["updated_date"],
            "Jun 24, 2026 01:33:27 PM EDT",
        )
        self.assertEqual(
            events[1].event_data["modification_description"],
            "Revised funding opportunity description",
        )
        self.assertEqual(
            events[2].event_data["modification_description"],
            "Updated anticipated posting date",
        )
        prepared = opportunities._prepare_history_events(events)
        self.assertEqual(prepared[1].timeline_title, "Grants.gov version update")
        self.assertEqual(prepared[1].timeline_version_name, "Synopsis 2")
        self.assertEqual(prepared[1].timeline_updated_label, "Jun 24, 2026")
        self.assertEqual(
            prepared[1].timeline_description,
            "Revised funding opportunity description",
        )
        self.assertEqual(prepared[1].timeline_modified_fields, ["Opportunity Title"])
        self.assertEqual(prepared[1].timeline_version_type, "Synopsis")
        self.assertEqual(prepared[1].timeline_source_revision, 2)


if __name__ == "__main__":
    unittest.main()
