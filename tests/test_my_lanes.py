import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import Organization, PursuitLane, PursuitLaneAssignment, User
from bidlens.services.pursuit_lanes import set_user_my_lanes, user_my_lanes


class MyLanesTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Lane Org", slug="lane-org")
        self.other_org = Organization(name="Other Org", slug="other-org")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.user = User(email="lane@example.com", name="Lane User", organization_id=self.org.id)
        self.db.add(self.user)
        self.db.flush()
        self.healthcare = PursuitLane(
            organization_id=self.org.id,
            name="Healthcare",
            agencies=[],
            naics=[],
            keywords=[],
            set_asides=[],
        )
        self.education = PursuitLane(
            organization_id=self.org.id,
            name="Education",
            agencies=[],
            naics=[],
            keywords=[],
            set_asides=[],
        )
        self.inactive = PursuitLane(
            organization_id=self.org.id,
            name="Inactive",
            agencies=[],
            naics=[],
            keywords=[],
            set_asides=[],
            is_active=False,
        )
        self.other_lane = PursuitLane(
            organization_id=self.other_org.id,
            name="Other Org Lane",
            agencies=[],
            naics=[],
            keywords=[],
            set_asides=[],
        )
        self.db.add_all([self.healthcare, self.education, self.inactive, self.other_lane])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_set_user_my_lanes_stores_user_preferences_for_org_lanes_only(self):
        saved_count = set_user_my_lanes(
            self.db,
            organization_id=self.org.id,
            user_id=self.user.id,
            lane_ids=[self.healthcare.id, self.education.id, self.other_lane.id, self.healthcare.id],
        )
        self.db.commit()

        lanes = user_my_lanes(self.db, organization_id=self.org.id, user_id=self.user.id)

        self.assertEqual(saved_count, 2)
        self.assertEqual([lane.name for lane in lanes], ["Education", "Healthcare"])
        self.assertEqual(self.db.query(PursuitLaneAssignment).count(), 2)

    def test_user_my_lanes_ignores_inactive_lanes_without_deleting_preference(self):
        self.db.add(PursuitLaneAssignment(
            organization_id=self.org.id,
            pursuit_lane_id=self.inactive.id,
            user_id=self.user.id,
        ))
        self.db.commit()

        lanes = user_my_lanes(self.db, organization_id=self.org.id, user_id=self.user.id)

        self.assertEqual(lanes, [])
        self.assertEqual(self.db.query(PursuitLaneAssignment).count(), 1)

    def test_set_user_my_lanes_does_not_save_inactive_lanes(self):
        saved_count = set_user_my_lanes(
            self.db,
            organization_id=self.org.id,
            user_id=self.user.id,
            lane_ids=[self.inactive.id],
        )
        self.db.commit()

        self.assertEqual(saved_count, 0)
        self.assertEqual(self.db.query(PursuitLaneAssignment).count(), 0)


if __name__ == "__main__":
    unittest.main()
