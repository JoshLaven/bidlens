import datetime as dt
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import (
    DailySnapshot,
    Event,
    IngestionRun,
    Opportunity,
    OpportunityPursuitLaneMatch,
    OpportunityUpdateEvent,
    Organization,
    OrganizationMembership,
    PursuitLane,
    PursuitLaneAssignment,
    User,
    Workspace,
)
from bidlens.services.daily_snapshot import create_daily_snapshot
from scripts.generate_daily_snapshots import format_snapshot


class DailySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Snapshot Org", slug="snapshot-org")
        self.db.add(self.org)
        self.db.flush()
        self.workspace = Workspace(
            organization_id=self.org.id,
            name="Snapshot Workspace",
            slug="snapshot-workspace",
        )
        self.user = User(email="snapshot@example.com", name="Snapshot User", organization_id=self.org.id)
        self.db.add_all([self.workspace, self.user])
        self.db.flush()
        self.db.add(OrganizationMembership(
            organization_id=self.org.id,
            user_id=self.user.id,
            role="admin",
        ))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _opportunity(self, record_id: str, created_at: dt.datetime, **overrides) -> Opportunity:
        values = {
            "organization_id": self.org.id,
            "source": "sam.gov",
            "source_record_id": record_id,
            "title": f"Opportunity {record_id}",
            "agency": "Test Agency",
            "opportunity_type": "Solicitation",
            "posted_date": created_at.date(),
            "response_deadline": dt.date(2026, 7, 10),
            "qualification_status": "qualified",
            "decision_state": "INBOX",
            "created_at": created_at,
        }
        values.update(overrides)
        opportunity = Opportunity(**values)
        self.db.add(opportunity)
        self.db.commit()
        return opportunity

    def test_create_daily_snapshot_uses_prior_calendar_day_and_stores_json(self):
        activity_day = dt.date(2026, 7, 7)
        snapshot_date = dt.date(2026, 7, 8)
        included = self._opportunity("INCLUDED", dt.datetime(2026, 7, 7, 10, 30))
        self._opportunity("EXCLUDED", dt.datetime(2026, 7, 8, 9, 0))

        update = OpportunityUpdateEvent(
            organization_id=self.org.id,
            opportunity_id=included.id,
            source="sam.gov",
            source_record_id=included.source_record_id,
            detected_at=dt.datetime(2026, 7, 7, 13, 0),
            changed_fields={"response_deadline": {"old": "2026-07-09", "new": "2026-07-10"}},
            salesforce_sync_status="not_synced",
        )
        self.db.add(update)
        self.db.add(Event(
            org_id=self.org.id,
            user_id=self.user.id,
            opp_id=included.id,
            event_type="vote_cast",
            ui_version="v1",
            ts=dt.datetime(2026, 7, 7, 14, 0),
            payload={"vote": "PURSUE", "requested_vote": "PURSUE", "toggled_off": False},
        ))
        self.db.add(IngestionRun(
            source="sam.gov",
            organization_id=self.org.id,
            user_id=self.user.id,
            started_at=dt.datetime(2026, 7, 7, 1, 0),
            finished_at=dt.datetime(2026, 7, 7, 1, 5),
            status="completed",
            error_count=0,
            processed_count=10,
            created_count=1,
            updated_count=1,
        ))
        self.db.commit()

        snapshot = create_daily_snapshot(
            self.db,
            workspace_id=self.workspace.id,
            user_id=self.user.id,
            snapshot_date=snapshot_date,
        )

        payload = snapshot.snapshot_json

        self.assertEqual(snapshot.status, "completed")
        self.assertEqual(payload["snapshot_date"], "2026-07-08")
        self.assertEqual(payload["activity_date"], activity_day.isoformat())
        self.assertEqual([item["source_record_id"] for item in payload["new_opportunities"]], ["INCLUDED"])
        self.assertEqual(payload["updated_opportunities"][0]["opportunity"]["source_record_id"], "INCLUDED")
        self.assertEqual(payload["interested_activity"][0]["opportunity"]["source_record_id"], "INCLUDED")
        self.assertEqual(payload["upcoming_deadlines"][0]["source_record_id"], "INCLUDED")
        self.assertEqual(payload["connector_issues"], [])
        self.assertNotIn("shortlisted_opportunities", payload)
        self.assertNotIn("team_activity", payload)
        self.assertNotIn("connector_status", payload)

    def test_daily_snapshot_includes_only_actionable_connector_issues(self):
        snapshot_date = dt.date(2026, 7, 8)
        self.db.add_all([
            IngestionRun(
                source="sam.gov",
                organization_id=self.org.id,
                user_id=self.user.id,
                started_at=dt.datetime(2026, 7, 7, 1, 0),
                finished_at=dt.datetime(2026, 7, 7, 1, 5),
                status="completed",
                error_count=0,
            ),
            IngestionRun(
                source="grants.gov",
                organization_id=self.org.id,
                user_id=self.user.id,
                started_at=dt.datetime(2026, 7, 7, 2, 0),
                finished_at=dt.datetime(2026, 7, 7, 2, 5),
                status="failed",
                error_count=1,
                notes="API unavailable",
            ),
        ])
        self.db.commit()

        snapshot = create_daily_snapshot(
            self.db,
            workspace_id=self.workspace.id,
            user_id=self.user.id,
            snapshot_date=snapshot_date,
        )

        issues = snapshot.snapshot_json["connector_issues"]

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["source_label"], "Grants.gov")
        self.assertTrue(issues[0]["needs_attention"])

    def test_daily_snapshot_includes_my_lane_context_without_limiting_org_activity(self):
        activity_day = dt.date(2026, 7, 7)
        snapshot_date = dt.date(2026, 7, 8)
        lane = PursuitLane(
            organization_id=self.org.id,
            name="Healthcare",
            agencies=[],
            naics=[],
            keywords=[],
            set_asides=[],
        )
        self.db.add(lane)
        self.db.flush()
        self.db.add(PursuitLaneAssignment(
            organization_id=self.org.id,
            pursuit_lane_id=lane.id,
            user_id=self.user.id,
        ))
        matched = self._opportunity(
            "MY-LANE",
            dt.datetime(2026, 7, 7, 9, 0),
            response_deadline=dt.date(2026, 7, 9),
        )
        other = self._opportunity(
            "ORG-WIDE",
            dt.datetime(2026, 7, 7, 10, 0),
            response_deadline=dt.date(2026, 7, 9),
        )
        self.db.add(OpportunityPursuitLaneMatch(
            organization_id=self.org.id,
            opportunity_id=matched.id,
            pursuit_lane_id=lane.id,
            matched_reasons=["keyword: healthcare"],
        ))
        self.db.add(OpportunityUpdateEvent(
            organization_id=self.org.id,
            opportunity_id=matched.id,
            source="sam.gov",
            source_record_id=matched.source_record_id,
            detected_at=dt.datetime.combine(activity_day, dt.time(hour=12)),
            changed_fields={"title": {"old": "Old", "new": "New"}},
            salesforce_sync_status="not_synced",
        ))
        self.db.commit()

        snapshot = create_daily_snapshot(
            self.db,
            workspace_id=self.workspace.id,
            user_id=self.user.id,
            snapshot_date=snapshot_date,
        )

        payload = snapshot.snapshot_json

        self.assertEqual([item["source_record_id"] for item in payload["new_opportunities"]], ["MY-LANE", "ORG-WIDE"])
        self.assertEqual(payload["my_shortlist"], [])
        self.assertEqual(payload["team_signals"], [])
        self.assertEqual(payload["my_lanes"], [])
        self.assertEqual(payload["my_lane_context"][0]["name"], "Healthcare")
        self.assertEqual(payload["my_lane_context"][0]["new_opportunity_count"], 1)
        self.assertEqual(payload["my_lane_context"][0]["updated_opportunity_count"], 1)
        self.assertEqual(payload["my_lane_context"][0]["upcoming_deadline_count"], 1)
        self.assertEqual(other.source_record_id, "ORG-WIDE")

    def test_create_daily_snapshot_returns_existing_snapshot_without_regenerating(self):
        snapshot_date = dt.date(2026, 7, 8)
        first = create_daily_snapshot(
            self.db,
            workspace_id=self.workspace.id,
            user_id=self.user.id,
            snapshot_date=snapshot_date,
        )
        self._opportunity("AFTER", dt.datetime(2026, 7, 7, 10, 30))

        second = create_daily_snapshot(
            self.db,
            workspace_id=self.workspace.id,
            user_id=self.user.id,
            snapshot_date=snapshot_date,
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(self.db.query(DailySnapshot).count(), 1)
        self.assertEqual(second.snapshot_json["new_opportunities"], [])

    def test_snapshot_inspector_formats_sections(self):
        snapshot = create_daily_snapshot(
            self.db,
            workspace_id=self.workspace.id,
            user_id=self.user.id,
            snapshot_date=dt.date(2026, 7, 8),
        )

        output = format_snapshot(snapshot)

        self.assertIn("Daily Snapshot", output)
        self.assertIn("New Opportunities", output)
        self.assertIn("Updated Opportunities", output)
        self.assertIn("Upcoming Deadlines", output)
        self.assertIn("Connector Issues", output)


if __name__ == "__main__":
    unittest.main()
