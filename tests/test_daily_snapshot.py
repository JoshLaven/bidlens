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
    Vote,
    Workspace,
)
from bidlens.services.daily_snapshot import create_daily_snapshot
from scripts.generate_daily_snapshots import format_snapshot, seed_qa_scenario


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
        self.member = User(email="member-snapshot@example.com", name="Snapshot Member", organization_id=self.org.id)
        self.db.add_all([self.workspace, self.user, self.member])
        self.db.flush()
        self.db.add_all([
            OrganizationMembership(
                organization_id=self.org.id,
                user_id=self.user.id,
                role="admin",
            ),
            OrganizationMembership(
                organization_id=self.org.id,
                user_id=self.member.id,
                role="member",
            ),
        ])
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
        self._opportunity("BEFORE", dt.datetime(2026, 7, 6, 23, 59, 59))
        start_boundary = self._opportunity("START", dt.datetime(2026, 7, 7, 0, 0))
        included = self._opportunity("INCLUDED", dt.datetime(2026, 7, 7, 10, 30))
        end_boundary = self._opportunity("END", dt.datetime(2026, 7, 7, 23, 59, 59))
        self._opportunity("EXCLUDED", dt.datetime(2026, 7, 8, 0, 0))

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
            user_id=self.member.id,
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
        self.assertEqual(
            [item["source_record_id"] for item in payload["new_opportunities"]],
            ["START", "INCLUDED", "END"],
        )
        self.assertEqual(payload["updated_opportunities"][0]["opportunity"]["source_record_id"], "INCLUDED")
        self.assertEqual(payload["interested_activity"][0]["opportunity"]["source_record_id"], "INCLUDED")
        self.assertIn(
            start_boundary.source_record_id,
            [item["source_record_id"] for item in payload["upcoming_deadlines"]],
        )
        self.assertIn(
            end_boundary.source_record_id,
            [item["source_record_id"] for item in payload["upcoming_deadlines"]],
        )
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

    def test_daily_snapshot_personalizes_shortlist_without_limiting_shared_activity(self):
        snapshot_date = dt.date(2026, 7, 8)
        activity_time = dt.datetime(2026, 7, 7, 9, 0)
        interested = self._opportunity("PERSONAL-A", activity_time)
        shared = self._opportunity("SHARED-NEW", dt.datetime(2026, 7, 7, 10, 0))
        tracked = self._opportunity("TRACKED", dt.datetime(2026, 7, 6, 10, 0))
        other_org = Organization(name="Other Snapshot Org", slug="other-snapshot-org")
        self.db.add(other_org)
        self.db.flush()
        other_workspace = Workspace(
            organization_id=other_org.id,
            name="Other Snapshot Workspace",
            slug="other-snapshot-workspace",
        )
        other_user = User(email="other-snapshot@example.com", organization_id=other_org.id)
        self.db.add_all([other_workspace, other_user])
        self.db.flush()
        self.db.add(OrganizationMembership(
            organization_id=other_org.id,
            user_id=other_user.id,
            role="member",
        ))
        self.db.add(Opportunity(
            organization_id=other_org.id,
            source="sam.gov",
            source_record_id="OTHER-WORKSPACE",
            title="Other Workspace Opportunity",
            agency="Other Agency",
            opportunity_type="Solicitation",
            posted_date=dt.date(2026, 7, 7),
            response_deadline=dt.date(2026, 7, 10),
            qualification_status="qualified",
            decision_state="INBOX",
            created_at=activity_time,
        ))
        self.db.add_all([
            Vote(
                org_id=self.org.id,
                user_id=self.user.id,
                opp_id=interested.id,
                vote="PURSUE",
                updated_at=dt.datetime(2026, 7, 7, 11, 0),
            ),
            Vote(
                org_id=self.org.id,
                user_id=self.user.id,
                opp_id=shared.id,
                vote="PURSUE",
                updated_at=dt.datetime(2026, 7, 6, 11, 0),
            ),
            Vote(
                org_id=self.org.id,
                user_id=self.user.id,
                opp_id=tracked.id,
                vote="PURSUE",
                updated_at=dt.datetime(2026, 7, 6, 12, 0),
            ),
            Vote(
                org_id=self.org.id,
                user_id=self.member.id,
                opp_id=tracked.id,
                vote="PURSUE",
                updated_at=dt.datetime(2026, 7, 7, 13, 0),
            ),
            Event(
                org_id=self.org.id,
                user_id=self.user.id,
                opp_id=interested.id,
                event_type="vote_cast",
                ui_version="v1",
                ts=dt.datetime(2026, 7, 7, 11, 0),
                payload={"vote": "PURSUE", "requested_vote": "PURSUE", "toggled_off": False},
            ),
            Event(
                org_id=self.org.id,
                user_id=self.member.id,
                opp_id=tracked.id,
                event_type="vote_cast",
                ui_version="v1",
                ts=dt.datetime(2026, 7, 7, 13, 0),
                payload={"vote": "PURSUE", "requested_vote": "PURSUE", "toggled_off": False},
            ),
        ])
        self.db.add(OpportunityUpdateEvent(
            organization_id=self.org.id,
            opportunity_id=shared.id,
            source="sam.gov",
            source_record_id=shared.source_record_id,
            detected_at=dt.datetime(2026, 7, 7, 12, 0),
            changed_fields={"title": {"old": "Old", "new": "New"}},
            salesforce_sync_status="not_synced",
        ))
        self.db.commit()

        user_snapshot = create_daily_snapshot(
            self.db,
            workspace_id=self.workspace.id,
            user_id=self.user.id,
            snapshot_date=snapshot_date,
        )
        member_snapshot = create_daily_snapshot(
            self.db,
            workspace_id=self.workspace.id,
            user_id=self.member.id,
            snapshot_date=snapshot_date,
        )

        user_payload = user_snapshot.snapshot_json
        member_payload = member_snapshot.snapshot_json

        self.assertEqual(user_payload["my_shortlist"][0]["opportunity"]["source_record_id"], "PERSONAL-A")
        self.assertNotIn(
            "PERSONAL-A",
            [item["opportunity"]["source_record_id"] for item in member_payload["my_shortlist"]],
        )
        self.assertEqual(user_payload["team_signals"][0]["opportunity"]["source_record_id"], "TRACKED")
        self.assertEqual(member_payload["team_signals"], [])
        self.assertEqual(user_payload["shortlist_updates"][0]["opportunity"]["source_record_id"], "SHARED-NEW")
        self.assertEqual(member_payload["shortlist_updates"], [])
        self.assertEqual(user_payload["summary"]["shortlist_update_count"], 1)
        self.assertEqual(user_payload["summary"]["team_signal_count"], 1)
        self.assertEqual(
            [item["source_record_id"] for item in user_payload["new_opportunities"]],
            ["PERSONAL-A", "SHARED-NEW"],
        )
        self.assertEqual(
            [item["source_record_id"] for item in member_payload["new_opportunities"]],
            ["PERSONAL-A", "SHARED-NEW"],
        )
        self.assertEqual(user_payload["updated_opportunities"][0]["opportunity"]["source_record_id"], "SHARED-NEW")
        self.assertNotIn(
            "OTHER-WORKSPACE",
            [item["source_record_id"] for item in user_payload["new_opportunities"]],
        )

    def test_updated_opportunities_are_deduplicated_by_opportunity(self):
        snapshot_date = dt.date(2026, 7, 8)
        opportunity = self._opportunity("UPDATED-ONCE", dt.datetime(2026, 7, 6, 9, 0))
        self.db.add_all([
            OpportunityUpdateEvent(
                organization_id=self.org.id,
                opportunity_id=opportunity.id,
                source="sam.gov",
                source_record_id=opportunity.source_record_id,
                detected_at=dt.datetime(2026, 7, 7, 10, 0),
                changed_fields={"title": {"old": "Old", "new": "New"}},
                salesforce_sync_status="not_synced",
            ),
            OpportunityUpdateEvent(
                organization_id=self.org.id,
                opportunity_id=opportunity.id,
                source="sam.gov",
                source_record_id=opportunity.source_record_id,
                detected_at=dt.datetime(2026, 7, 7, 11, 0),
                changed_fields={"response_deadline": {"old": "2026-07-09", "new": "2026-07-10"}},
                salesforce_sync_status="not_synced",
            ),
        ])
        self.db.commit()

        snapshot = create_daily_snapshot(
            self.db,
            workspace_id=self.workspace.id,
            user_id=self.user.id,
            snapshot_date=snapshot_date,
        )

        updates = snapshot.snapshot_json["updated_opportunities"]

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["opportunity"]["source_record_id"], "UPDATED-ONCE")
        self.assertEqual(updates[0]["update_count"], 2)
        self.assertEqual(set(updates[0]["changed_fields"]), {"title", "response_deadline"})

    def test_out_of_window_and_non_meaningful_updates_are_excluded(self):
        snapshot_date = dt.date(2026, 7, 8)
        outside = self._opportunity("OUTSIDE-UPDATE", dt.datetime(2026, 7, 6, 9, 0))
        empty = self._opportunity("EMPTY-UPDATE", dt.datetime(2026, 7, 6, 10, 0))
        self.db.add_all([
            OpportunityUpdateEvent(
                organization_id=self.org.id,
                opportunity_id=outside.id,
                source="sam.gov",
                source_record_id=outside.source_record_id,
                detected_at=dt.datetime(2026, 7, 8, 0, 0),
                changed_fields={"title": {"old": "Old", "new": "New"}},
                salesforce_sync_status="not_synced",
            ),
            OpportunityUpdateEvent(
                organization_id=self.org.id,
                opportunity_id=empty.id,
                source="sam.gov",
                source_record_id=empty.source_record_id,
                detected_at=dt.datetime(2026, 7, 7, 12, 0),
                changed_fields={},
                salesforce_sync_status="not_synced",
            ),
        ])
        self.db.commit()

        snapshot = create_daily_snapshot(
            self.db,
            workspace_id=self.workspace.id,
            user_id=self.user.id,
            snapshot_date=snapshot_date,
        )

        record_ids = [
            item["opportunity"]["source_record_id"]
            for item in snapshot.snapshot_json["updated_opportunities"]
        ]

        self.assertNotIn("OUTSIDE-UPDATE", record_ids)
        self.assertNotIn("EMPTY-UPDATE", record_ids)

    def test_shortlist_changes_are_user_specific_and_include_removal(self):
        snapshot_date = dt.date(2026, 7, 8)
        added = self._opportunity("SHORTLIST-ADD", dt.datetime(2026, 7, 6, 9, 0))
        removed = self._opportunity("SHORTLIST-REMOVE", dt.datetime(2026, 7, 6, 10, 0))
        teammate = self._opportunity("TEAMMATE-ONLY", dt.datetime(2026, 7, 6, 11, 0))
        self.db.add_all([
            Vote(
                org_id=self.org.id,
                user_id=self.user.id,
                opp_id=added.id,
                vote="PURSUE",
                updated_at=dt.datetime(2026, 7, 7, 9, 0),
            ),
            Event(
                org_id=self.org.id,
                user_id=self.user.id,
                opp_id=added.id,
                event_type="vote_cast",
                ui_version="v1",
                ts=dt.datetime(2026, 7, 7, 9, 0),
                payload={"vote": "PURSUE", "requested_vote": "PURSUE", "toggled_off": False},
            ),
            Event(
                org_id=self.org.id,
                user_id=self.user.id,
                opp_id=removed.id,
                event_type="vote_cast",
                ui_version="v1",
                ts=dt.datetime(2026, 7, 7, 10, 0),
                payload={"vote": None, "requested_vote": "PURSUE", "toggled_off": True},
            ),
            Event(
                org_id=self.org.id,
                user_id=self.member.id,
                opp_id=teammate.id,
                event_type="vote_cast",
                ui_version="v1",
                ts=dt.datetime(2026, 7, 7, 11, 0),
                payload={"vote": "PURSUE", "requested_vote": "PURSUE", "toggled_off": False},
            ),
        ])
        self.db.commit()

        snapshot = create_daily_snapshot(
            self.db,
            workspace_id=self.workspace.id,
            user_id=self.user.id,
            snapshot_date=snapshot_date,
        )

        changes = snapshot.snapshot_json["shortlist_changes"]
        by_record = {
            item["opportunity"]["source_record_id"]: item
            for item in changes
        }

        self.assertEqual(set(by_record), {"SHORTLIST-ADD", "SHORTLIST-REMOVE"})
        self.assertEqual(by_record["SHORTLIST-ADD"]["change_type"], "added")
        self.assertEqual(by_record["SHORTLIST-REMOVE"]["change_type"], "removed")
        self.assertEqual(snapshot.snapshot_json["my_shortlist"][0]["opportunity"]["source_record_id"], "SHORTLIST-ADD")

    def test_interested_activity_is_teammate_only_and_deduped(self):
        snapshot_date = dt.date(2026, 7, 8)
        tracked = self._opportunity("TRACKED-TEAM", dt.datetime(2026, 7, 6, 9, 0))
        self.db.add_all([
            Event(
                org_id=self.org.id,
                user_id=self.user.id,
                opp_id=tracked.id,
                event_type="vote_cast",
                ui_version="v1",
                ts=dt.datetime(2026, 7, 7, 9, 0),
                payload={"vote": "PURSUE", "requested_vote": "PURSUE", "toggled_off": False},
            ),
            Event(
                org_id=self.org.id,
                user_id=self.member.id,
                opp_id=tracked.id,
                event_type="vote_cast",
                ui_version="v1",
                ts=dt.datetime(2026, 7, 7, 10, 0),
                payload={"vote": "PURSUE", "requested_vote": "PURSUE", "toggled_off": False},
            ),
            Event(
                org_id=self.org.id,
                user_id=self.member.id,
                opp_id=tracked.id,
                event_type="vote_cast",
                ui_version="v1",
                ts=dt.datetime(2026, 7, 7, 11, 0),
                payload={"vote": "PURSUE", "requested_vote": "PURSUE", "toggled_off": False},
            ),
            Event(
                org_id=self.org.id,
                user_id=self.member.id,
                opp_id=tracked.id,
                event_type="vote_cast",
                ui_version="v1",
                ts=dt.datetime(2026, 7, 7, 12, 0),
                payload={"vote": None, "requested_vote": "PURSUE", "toggled_off": True},
            ),
        ])
        self.db.commit()

        snapshot = create_daily_snapshot(
            self.db,
            workspace_id=self.workspace.id,
            user_id=self.user.id,
            snapshot_date=snapshot_date,
        )

        activity = snapshot.snapshot_json["interested_activity"]

        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["user"]["id"], self.member.id)
        self.assertEqual(activity[0]["opportunity"]["source_record_id"], "TRACKED-TEAM")

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

    def test_qa_seed_multiple_signals_creates_expected_snapshot_sections(self):
        snapshot_date = dt.date(2026, 7, 8)

        result = seed_qa_scenario(
            self.db,
            scenario="multiple-signals",
            snapshot_date=snapshot_date,
            workspace_id=self.workspace.id,
            user_email=self.user.email,
        )
        snapshot = create_daily_snapshot(
            self.db,
            workspace_id=self.workspace.id,
            user_id=self.user.id,
            snapshot_date=snapshot_date,
        )
        payload = snapshot.snapshot_json

        self.assertEqual(result["scenario"], "multiple-signals")
        self.assertEqual(len(payload["updated_opportunities"]), 1)
        self.assertEqual(payload["updated_opportunities"][0]["update_count"], 2)
        self.assertEqual(len(payload["shortlist_changes"]), 1)
        self.assertEqual(payload["shortlist_changes"][0]["change_type"], "added")
        self.assertEqual(len(payload["interested_activity"]), 1)
        self.assertEqual(len(payload["team_signals"]), 1)


if __name__ == "__main__":
    unittest.main()
