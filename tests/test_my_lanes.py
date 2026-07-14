import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import Organization, OrganizationMembership, PursuitLane, PursuitLaneAssignment, User
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
        self.db.add(OrganizationMembership(
            organization_id=self.org.id,
            user_id=self.user.id,
            role="member",
        ))
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
        setattr(self.user, "current_organization_id", self.org.id)
        setattr(self.user, "current_role", "member")

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

    def test_queue_toolbar_uses_single_lane_dropdown(self):
        template = Path("src/bidlens/templates/_queue_layout.html").read_text()

        self.assertIn('queue-toolbar-column queue-toolbar-column--sort', template)
        self.assertIn('queue-toolbar-column queue-toolbar-column--filters', template)
        self.assertIn('show_filter_controls = show_filters', template)
        self.assertIn('class="queue-lane-filter-form"', template)
        self.assertIn('<label for="{{ id_prefix }}-lane">Lane</label>', template)
        self.assertIn('<option value="my_lanes"', template)
        self.assertIn('<optgroup label="All Workspace Lanes">', template)
        self.assertNotIn('class="queue-lane-row"', template)
        self.assertNotIn('feed-toolbar-label">My Lanes</span>', template)

    def test_feed_toolbar_is_role_aware(self):
        feed_template = Path("src/bidlens/templates/feed.html").read_text()
        toolbar_template = Path("src/bidlens/templates/_queue_layout.html").read_text()

        self.assertIn("show_filters=(user.current_role == 'admin')", feed_template)
        self.assertIn("feed_sort_options=true", feed_template)
        self.assertIn("queue-sort-direction-toggle", toolbar_template)
        self.assertIn(">Imported</option>", toolbar_template)
        self.assertNotIn("Imported Date", toolbar_template)
        self.assertNotIn('id="{{ id_prefix }}-sort-direction"', toolbar_template)
        self.assertIn("{% if not feed_sort_options %}", toolbar_template)


if __name__ == "__main__":
    unittest.main()
