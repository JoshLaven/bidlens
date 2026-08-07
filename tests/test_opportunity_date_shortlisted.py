import importlib.util
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import Opportunity, Organization, User
from bidlens.services import cast_vote, transition_state
from bidlens.services.shortlisting import ensure_user_shortlisted
from bidlens.state_machine import OppState


MIGRATION_PATH = Path(
    "alembic/versions/b2c3d4e5f6a8_add_opportunity_date_shortlisted.py"
)


class OpportunityDateShortlistedTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Shortlist Org", slug="shortlist-date-org")
        self.user = User(email="shortlist@example.com", organization=self.org)
        self.db.add_all((self.org, self.user))
        self.db.flush()
        self.opportunity = Opportunity(
            organization_id=self.org.id,
            source="manual",
            source_record_id="shortlist-date-1",
            title="First shortlist date",
            agency="Agency",
            opportunity_type="RFP",
            posted_date=date(2026, 8, 1),
            response_deadline=date(2026, 9, 1),
            qualification_status="qualified",
        )
        self.db.add(self.opportunity)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_feed_qualification_sets_once_and_pass_restore_preserves_original(self):
        first = datetime(2026, 8, 1, 9, tzinfo=timezone.utc)
        later = datetime(2026, 8, 2, 9, tzinfo=timezone.utc)

        self.assertTrue(ensure_user_shortlisted(
            self.db, opportunity=self.opportunity, user=self.user, now=first
        ))
        self.db.commit()
        cast_vote(
            self.db, org_id=self.org.id, user_id=self.user.id,
            opp_id=self.opportunity.id, vote="PASS", toggle_existing=False,
        )
        self.assertTrue(ensure_user_shortlisted(
            self.db, opportunity=self.opportunity, user=self.user, now=later
        ))
        self.db.commit()

        self.db.refresh(self.opportunity)
        self.assertEqual(
            self.opportunity.date_shortlisted.replace(tzinfo=timezone.utc), first
        )

    def test_org_state_shortlist_entry_sets_once_and_archive_preserves_it(self):
        transition_state(
            self.db, org_id=self.org.id, user_id=self.user.id,
            opp_id=self.opportunity.id, to_state=OppState.SHORTLISTED,
        )
        self.db.refresh(self.opportunity)
        first = self.opportunity.date_shortlisted
        self.assertIsNotNone(first)

        transition_state(
            self.db, org_id=self.org.id, user_id=self.user.id,
            opp_id=self.opportunity.id, to_state=OppState.ARCHIVED,
        )
        self.db.refresh(self.opportunity)
        self.assertEqual(self.opportunity.date_shortlisted, first)

    def test_existing_shortlisted_signal_is_not_used_as_a_backfill(self):
        ensure_user_shortlisted(
            self.db, opportunity=self.opportunity, user=self.user,
            now=datetime(2026, 8, 1, 9, tzinfo=timezone.utc),
        )
        self.opportunity.date_shortlisted = None
        self.db.commit()

        self.assertFalse(ensure_user_shortlisted(
            self.db, opportunity=self.opportunity, user=self.user,
            now=datetime(2026, 8, 3, 9, tzinfo=timezone.utc),
        ))
        self.assertIsNone(self.opportunity.date_shortlisted)

    def test_model_and_migration_are_nullable_additive_and_do_not_backfill(self):
        column = Opportunity.__table__.c.date_shortlisted
        self.assertTrue(column.nullable)
        self.assertTrue(column.type.timezone)

        spec = importlib.util.spec_from_file_location("date_shortlisted_migration", MIGRATION_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        self.assertEqual(module.down_revision, "a1b2c3d4e5a7")

        with (
            patch.object(module.op, "add_column") as add_column,
            patch.object(module.op, "execute") as execute,
        ):
            module.upgrade()
        added = add_column.call_args.args[1]
        self.assertEqual(added.name, "date_shortlisted")
        self.assertTrue(added.nullable)
        self.assertTrue(added.type.timezone)
        execute.assert_not_called()

        with patch.object(module.op, "drop_column") as drop_column:
            module.downgrade()
        drop_column.assert_called_once_with("opportunities", "date_shortlisted")


if __name__ == "__main__":
    unittest.main()
