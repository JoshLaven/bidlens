import datetime as dt
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import (
    Opportunity,
    OpportunityHistoryEvent,
    OpportunityHistoryRecipient,
    Organization,
    User,
    Vote,
)
from bidlens.routes import opportunities
from bidlens.services.opportunity_history import (
    EVENT_IMPORTED,
    EVENT_SALESFORCE_SYNCHRONIZED,
    EVENT_SOURCE_UPDATED,
    mark_history_read,
    record_history_event,
    record_imported_history,
    unread_history_count,
)
from bidlens.services.opportunity_monitor import apply_source_update


class OpportunityHistoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="History Org", slug="history-org")
        self.db.add(self.org)
        self.db.flush()
        self.interested = User(email="interested@example.com", organization_id=self.org.id)
        self.passed = User(email="passed@example.com", organization_id=self.org.id)
        self.untouched = User(email="untouched@example.com", organization_id=self.org.id)
        self.db.add_all([self.interested, self.passed, self.untouched])
        self.db.flush()
        self.opportunity = Opportunity(
            organization_id=self.org.id,
            source="sam",
            source_record_id="history-1",
            title="History opportunity",
            agency="History Agency",
            opportunity_type="Solicitation",
            posted_date=dt.date.today(),
            response_deadline=dt.date.today() + dt.timedelta(days=30),
            qualification_status="qualified",
            decision_state="INBOX",
        )
        self.db.add(self.opportunity)
        self.db.flush()
        self.db.add_all([
            Vote(
                org_id=self.org.id,
                opp_id=self.opportunity.id,
                user_id=self.interested.id,
                vote="PURSUE",
            ),
            Vote(
                org_id=self.org.id,
                opp_id=self.opportunity.id,
                user_id=self.passed.id,
                vote="PASS",
            ),
        ])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_history_event_notifies_only_interested_users(self):
        event = record_history_event(
            self.db,
            opportunity=self.opportunity,
            event_type=EVENT_SOURCE_UPDATED,
            source="sam",
        )
        self.db.commit()

        recipients = self.db.query(OpportunityHistoryRecipient).all()
        self.assertEqual([(row.history_event_id, row.user_id) for row in recipients], [
            (event.id, self.interested.id),
        ])
        self.assertEqual(
            unread_history_count(
                self.db,
                organization_id=self.org.id,
                opportunity_id=self.opportunity.id,
                user_id=self.interested.id,
            ),
            1,
        )
        self.assertEqual(
            unread_history_count(
                self.db,
                organization_id=self.org.id,
                opportunity_id=self.opportunity.id,
                user_id=self.passed.id,
            ),
            0,
        )
        self.assertEqual(
            unread_history_count(
                self.db,
                organization_id=self.org.id,
                opportunity_id=self.opportunity.id,
                user_id=self.untouched.id,
            ),
            0,
        )

    def test_opening_history_marks_only_current_users_events_read(self):
        record_history_event(
            self.db,
            opportunity=self.opportunity,
            event_type=EVENT_SOURCE_UPDATED,
            source="sam",
        )
        self.db.commit()

        updated = mark_history_read(
            self.db,
            organization_id=self.org.id,
            opportunity_id=self.opportunity.id,
            user_id=self.interested.id,
        )

        self.assertEqual(updated, 1)
        self.assertEqual(
            unread_history_count(
                self.db,
                organization_id=self.org.id,
                opportunity_id=self.opportunity.id,
                user_id=self.interested.id,
            ),
            0,
        )

    def test_import_event_does_not_notify_users(self):
        event = record_imported_history(self.db, self.opportunity)
        self.db.commit()

        self.assertEqual(event.event_type, EVENT_IMPORTED)
        self.assertEqual(self.db.query(OpportunityHistoryRecipient).count(), 0)

    @patch("bidlens.services.opportunity_monitor.SalesforceService")
    def test_source_update_and_salesforce_sync_create_history(self, service_class):
        self.opportunity.salesforce_opportunity_id = "006HISTORY"
        self.db.commit()

        result = apply_source_update(
            self.db,
            self.opportunity,
            {"title": "Updated history opportunity"},
            observed_at=dt.datetime(2026, 7, 4, 9, 30),
        )
        self.db.commit()

        self.assertTrue(result.changed)
        self.assertEqual(
            [
                event.event_type
                for event in (
                    self.db.query(OpportunityHistoryEvent)
                    .order_by(OpportunityHistoryEvent.id.asc())
                    .all()
                )
            ],
            [EVENT_SOURCE_UPDATED, EVENT_SALESFORCE_SYNCHRONIZED],
        )
        self.assertEqual(self.db.query(OpportunityHistoryRecipient).count(), 2)
        self.assertEqual(self.opportunity.qualification_status, "qualified")
        self.assertEqual(self.opportunity.decision_state, "INBOX")

        feed_user = SimpleNamespace(
            id=self.untouched.id,
            organization_id=self.org.id,
            current_organization_id=self.org.id,
        )
        feed_ids = {
            opportunity.id
            for opportunity, _watched in opportunities._feed_query(
                self.db,
                feed_user,
            ).all()
        }
        self.assertIn(self.opportunity.id, feed_ids)

    def test_source_update_history_prepares_summary_and_change_details(self):
        result = apply_source_update(
            self.db,
            self.opportunity,
            {
                "response_deadline": dt.date.today() + dt.timedelta(days=45),
                "description_text": "Updated synopsis",
            },
            observed_at=dt.datetime(2026, 7, 4, 9, 30),
        )
        self.db.commit()

        self.assertTrue(result.changed)
        event = self.db.query(OpportunityHistoryEvent).one()
        prepared = opportunities._prepare_history_events([event])[0]
        self.assertEqual(prepared.timeline_title, "Opportunity updated from SAM.gov")
        self.assertEqual(
            set(prepared.timeline_modified_fields),
            {"Due date", "Synopsis"},
        )
        self.assertIn("changed", prepared.timeline_description)
        self.assertEqual(len(prepared.timeline_change_details), 2)
        self.assertIn(
            "Synopsis updated",
            [change["summary"] for change in prepared.timeline_change_details],
        )


if __name__ == "__main__":
    unittest.main()
