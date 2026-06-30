import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import Opportunity, Organization, User, Vote
from bidlens.routes import api
from bidlens.services import cast_vote


class BulkArchiveTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        session_factory = sessionmaker(bind=self.engine)
        self.db = session_factory()

        org = Organization(name="Test Org", slug="bulk-archive-test")
        self.db.add(org)
        self.db.flush()
        self.user = User(email="bulk@example.com", organization_id=org.id)
        self.db.add(self.user)
        self.db.flush()

        self.opportunities = [
            Opportunity(
                organization_id=org.id,
                source="test",
                source_record_id=f"bulk-{index}",
                title=f"Bulk opportunity {index}",
                agency="Test Agency",
                opportunity_type="Solicitation",
                posted_date=date.today(),
                response_deadline=date.today() + timedelta(days=30),
                qualification_status="qualified",
            )
            for index in range(2)
        ]
        self.db.add_all(self.opportunities)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_individual_pass_remains_toggleable(self):
        opp = self.opportunities[0]

        first = cast_vote(
            self.db,
            org_id=self.user.organization_id,
            user_id=self.user.id,
            opp_id=opp.id,
            vote="PASS",
        )
        second = cast_vote(
            self.db,
            org_id=self.user.organization_id,
            user_id=self.user.id,
            opp_id=opp.id,
            vote="PASS",
        )

        self.assertEqual(first["vote"], "PASS")
        self.assertIsNone(second["vote"])
        self.assertTrue(second["toggled_off"])

    def test_bulk_archive_sets_pass_for_every_selected_opportunity(self):
        opp_ids = [opp.id for opp in self.opportunities]
        payload = api.BulkPassIn(opp_ids=opp_ids)

        with patch.object(api, "require_user", return_value=self.user):
            result = api.api_bulk_pass(payload, MagicMock(), self.db)

        votes = (
            self.db.query(Vote)
            .filter(
                Vote.user_id == self.user.id,
                Vote.opp_id.in_(opp_ids),
            )
            .all()
        )
        self.assertEqual(result["archived_count"], 2)
        self.assertEqual(result["archived_opp_ids"], opp_ids)
        self.assertEqual({vote.vote for vote in votes}, {"PASS"})

    def test_bulk_archive_is_idempotent_for_existing_pass(self):
        opp = self.opportunities[0]
        cast_vote(
            self.db,
            org_id=self.user.organization_id,
            user_id=self.user.id,
            opp_id=opp.id,
            vote="PASS",
        )

        with patch.object(api, "require_user", return_value=self.user):
            api.api_bulk_pass(api.BulkPassIn(opp_ids=[opp.id]), MagicMock(), self.db)

        vote = (
            self.db.query(Vote)
            .filter(
                Vote.user_id == self.user.id,
                Vote.opp_id == opp.id,
            )
            .one()
        )
        self.assertEqual(vote.vote, "PASS")


if __name__ == "__main__":
    unittest.main()
