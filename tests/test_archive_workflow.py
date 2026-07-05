import asyncio
import unittest
from contextlib import ExitStack
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import Opportunity, Organization, User, Vote
from bidlens.routes import opportunities
from bidlens.services import cast_vote


class ArchiveQueryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        session_factory = sessionmaker(bind=self.engine)
        self.db = session_factory()

        org = Organization(name="Test Org", slug="archive-test")
        self.db.add(org)
        self.db.flush()
        self.user = User(email="member@example.com", organization_id=org.id)
        self.other_user = User(email="other@example.com", organization_id=org.id)
        self.db.add_all([self.user, self.other_user])
        self.db.flush()
        self.opp = Opportunity(
            organization_id=org.id,
            source="test",
            source_record_id="archive-1",
            title="Archived opportunity",
            agency="Test Agency",
            opportunity_type="Solicitation",
            posted_date=date.today(),
            response_deadline=date.today() + timedelta(days=30),
            qualification_status="qualified",
        )
        self.db.add(self.opp)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _query_ids(self, query):
        return {opp.id for opp, _watched in query.all()}

    def test_pass_moves_opportunity_from_feed_to_current_user_archive(self):
        cast_vote(
            self.db,
            org_id=self.user.organization_id,
            user_id=self.user.id,
            opp_id=self.opp.id,
            vote="PASS",
        )

        feed_ids = self._query_ids(
            opportunities._feed_query(self.db, self.user, "solicitations")
        )
        archive_ids = self._query_ids(
            opportunities._user_archive_query(self.db, self.user, "solicitations")
        )

        self.assertNotIn(self.opp.id, feed_ids)
        self.assertIn(self.opp.id, archive_ids)

    def test_archive_is_scoped_to_current_user(self):
        cast_vote(
            self.db,
            org_id=self.other_user.organization_id,
            user_id=self.other_user.id,
            opp_id=self.opp.id,
            vote="PASS",
        )

        archive_ids = self._query_ids(
            opportunities._user_archive_query(self.db, self.user, "solicitations")
        )

        self.assertNotIn(self.opp.id, archive_ids)

    def test_restore_clears_pass_and_returns_opportunity_to_feed(self):
        cast_vote(
            self.db,
            org_id=self.user.organization_id,
            user_id=self.user.id,
            opp_id=self.opp.id,
            vote="PASS",
        )
        restored = cast_vote(
            self.db,
            org_id=self.user.organization_id,
            user_id=self.user.id,
            opp_id=self.opp.id,
            vote="PASS",
        )

        feed_ids = self._query_ids(
            opportunities._feed_query(self.db, self.user, "solicitations")
        )
        archive_ids = self._query_ids(
            opportunities._user_archive_query(self.db, self.user, "solicitations")
        )

        self.assertTrue(restored["toggled_off"])
        self.assertIn(self.opp.id, feed_ids)
        self.assertNotIn(self.opp.id, archive_ids)


class FeedRouteArchiveTests(unittest.TestCase):
    def test_legacy_show_passed_parameter_does_not_change_active_feed_query(self):
        user = SimpleNamespace(id=1, organization_id=1, triage_enabled=True)
        db = MagicMock()
        query = MagicMock()
        query.count.return_value = 0
        query.limit.return_value.all.return_value = []

        identity_helpers = (
            "apply_org_filters",
            "_apply_lane_filter",
            "_apply_feed_search",
            "_apply_past_due_filter",
            "_apply_feed_ordering",
        )
        patches = [
            patch.object(opportunities, "require_user", return_value=user),
            patch.object(opportunities, "_feed_query", return_value=query),
            patch.object(opportunities, "_enrich_opps", return_value=[]),
            patch.object(opportunities, "get_sidebar", return_value={}),
            patch.object(opportunities, "_active_lanes", return_value=[]),
            patch.object(opportunities.templates, "TemplateResponse", return_value=MagicMock()),
        ]
        patches.extend(
            patch.object(
                opportunities,
                helper,
                side_effect=lambda current_query, *args, **kwargs: current_query,
            )
            for helper in identity_helpers
        )

        with ExitStack() as stack:
            entered_patches = [stack.enter_context(item) for item in patches]
            feed_query = entered_patches[1]
            asyncio.run(
                opportunities.feed(
                    request=MagicMock(),
                    show_passed="1",
                    db=db,
                )
            )

        feed_query.assert_called_once_with(db, user)


if __name__ == "__main__":
    unittest.main()
