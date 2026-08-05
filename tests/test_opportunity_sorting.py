import unittest
import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import (
    Opportunity,
    OpportunityPursuitLaneMatch,
    Organization,
    PursuitLane,
    User,
    Vote,
)
from bidlens.routes.opportunities import (
    _apply_feed_ordering,
    _normalize_feed_sort,
)
from bidlens.routes import opportunities as opportunity_routes
from bidlens.services import cast_vote
from bidlens.services.shortlisting import ensure_user_shortlisted


class OpportunitySortingTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Sorting Org", slug="sorting-org")
        self.other_org = Organization(name="Other Org", slug="other-sorting-org")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.user = User(email="sorter@example.com", organization_id=self.org.id)
        self.db.add(self.user)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _opportunity(self, title, *, imported):
        opportunity = Opportunity(
            organization_id=self.org.id,
            source="manual",
            source_record_id=f"sort-{title.lower().replace(' ', '-')}",
            title=title,
            agency="Agency",
            opportunity_type="Solicitation",
            posted_date=date(2026, 8, 1),
            response_deadline=date(2026, 9, 1),
            qualification_status="qualified",
            upserted_at=imported,
        )
        self.db.add(opportunity)
        self.db.flush()
        return opportunity

    def _lane(self, name, *, organization=None, active=True):
        lane = PursuitLane(
            organization_id=(organization or self.org).id,
            name=name,
            is_active=active,
        )
        self.db.add(lane)
        self.db.flush()
        return lane

    def _match(self, opportunity, lane, *, organization=None):
        self.db.add(
            OpportunityPursuitLaneMatch(
                organization_id=(organization or self.org).id,
                opportunity_id=opportunity.id,
                pursuit_lane_id=lane.id,
                matched_reasons=[],
            )
        )

    def test_lane_sort_uses_first_active_tenant_lane_and_direction_only_moves_unassigned(self):
        old = datetime(2026, 8, 1)
        health_newer = self._opportunity("Health newer", imported=old + timedelta(days=3))
        health_older = self._opportunity("Health older", imported=old + timedelta(days=1))
        data = self._opportunity("Data", imported=old + timedelta(days=2))
        unassigned = self._opportunity("Unassigned", imported=old + timedelta(days=4))

        health = self._lane("health")
        data_lane = self._lane("Data")
        inactive = self._lane("Aardvark", active=False)
        foreign = self._lane("Alpha", organization=self.other_org)
        self._match(health_newer, health)
        self._match(health_older, health)
        self._match(data, health)
        self._match(data, data_lane)  # Representative value is Data, not Health.
        self._match(unassigned, inactive)
        self._match(unassigned, foreign, organization=self.other_org)
        self.db.commit()

        base = self.db.query(Opportunity).filter(Opportunity.organization_id == self.org.id)
        ascending = _apply_feed_ordering(
            base, sort="lane", direction="asc", organization_id=self.org.id
        ).all()
        descending = _apply_feed_ordering(
            base, sort="lane", direction="desc", organization_id=self.org.id
        ).all()

        self.assertEqual(
            [row.title for row in ascending],
            ["Data", "Health newer", "Health older", "Unassigned"],
        )
        self.assertEqual(
            [row.title for row in descending],
            ["Unassigned", "Data", "Health newer", "Health older"],
        )

    def test_lane_ordering_occurs_before_pagination(self):
        first = self._opportunity("First", imported=datetime(2026, 8, 1))
        second = self._opportunity("Second", imported=datetime(2026, 8, 2))
        third = self._opportunity("Third", imported=datetime(2026, 8, 3))
        alpha = self._lane("Alpha")
        zulu = self._lane("Zulu")
        self._match(first, zulu)
        self._match(second, alpha)
        self._match(third, zulu)
        self.db.commit()

        page = _apply_feed_ordering(
            self.db.query(Opportunity),
            sort="lane",
            direction="asc",
            organization_id=self.org.id,
        ).offset(1).limit(1).all()
        self.assertEqual([row.title for row in page], ["Third"])

    def test_requested_sort_replaces_upstream_default_ordering(self):
        alpha = self._opportunity("Alpha lane", imported=datetime(2026, 8, 1))
        zulu = self._opportunity("Zulu lane", imported=datetime(2026, 8, 3))
        alpha_lane = self._lane("Alpha")
        zulu_lane = self._lane("Zulu")
        self._match(alpha, alpha_lane)
        self._match(zulu, zulu_lane)
        self.db.commit()

        preordered = self.db.query(Opportunity).order_by(Opportunity.upserted_at.desc())
        rows = _apply_feed_ordering(
            preordered,
            sort="lane",
            direction="asc",
            organization_id=self.org.id,
        ).all()
        self.assertEqual([row.title for row in rows], ["Alpha lane", "Zulu lane"])

    def test_date_shortlisted_orders_known_values_then_null_in_both_directions(self):
        oldest = self._opportunity("Oldest", imported=datetime(2026, 8, 1))
        newest = self._opportunity("Newest", imported=datetime(2026, 8, 2))
        historical = self._opportunity("Historical", imported=datetime(2026, 8, 3))
        self.db.add_all(
            [
                Vote(org_id=self.org.id, user_id=self.user.id, opp_id=oldest.id, vote="PURSUE", shortlisted_at=datetime(2026, 7, 1, tzinfo=timezone.utc)),
                Vote(org_id=self.org.id, user_id=self.user.id, opp_id=newest.id, vote="PURSUE", shortlisted_at=datetime(2026, 8, 1, tzinfo=timezone.utc)),
                Vote(org_id=self.org.id, user_id=self.user.id, opp_id=historical.id, vote="PURSUE", shortlisted_at=None),
            ]
        )
        self.db.commit()
        base = self.db.query(Opportunity).join(Vote).filter(
            Vote.org_id == self.org.id,
            Vote.user_id == self.user.id,
            Vote.vote == "PURSUE",
        )

        ascending = _apply_feed_ordering(
            base, sort="shortlisted", direction="asc", organization_id=self.org.id,
            allow_shortlisted=True,
        ).all()
        descending = _apply_feed_ordering(
            base, sort="shortlisted", direction="desc", organization_id=self.org.id,
            allow_shortlisted=True,
        ).all()
        self.assertEqual([row.title for row in ascending], ["Oldest", "Newest", "Historical"])
        self.assertEqual([row.title for row in descending], ["Newest", "Oldest", "Historical"])

    def test_shortlisted_sort_is_only_allowlisted_for_shortlist(self):
        self.assertEqual(_normalize_feed_sort("shortlisted"), "imported")
        self.assertEqual(_normalize_feed_sort("shortlisted", allow_shortlisted=True), "shortlisted")
        self.assertEqual(_normalize_feed_sort("invalid", allow_shortlisted=True), "imported")
        self.assertEqual(_normalize_feed_sort("lane"), "lane")


class ShortlistedTimestampTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Vote Org", slug="vote-org")
        self.db.add(self.org)
        self.db.flush()
        self.user = User(email="voter@example.com", organization_id=self.org.id)
        self.opportunity = Opportunity(
            organization_id=self.org.id,
            source="manual",
            source_record_id="vote-transition",
            title="Vote transition",
            agency="Agency",
            opportunity_type="Solicitation",
            posted_date=date(2026, 8, 1),
            response_deadline=date(2026, 9, 1),
            qualification_status="qualified",
        )
        self.db.add_all([self.user, self.opportunity])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_transition_semantics_set_preserve_retain_and_replace(self):
        first = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
        second = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
        third = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)

        self.assertTrue(ensure_user_shortlisted(
            self.db, opportunity=self.opportunity, user=self.user, now=first
        ))
        vote = self.db.query(Vote).one()
        self.assertEqual(vote.shortlisted_at.replace(tzinfo=timezone.utc), first)

        self.assertFalse(ensure_user_shortlisted(
            self.db, opportunity=self.opportunity, user=self.user, now=second
        ))
        self.assertEqual(vote.shortlisted_at.replace(tzinfo=timezone.utc), first)

        vote.vote = "PASS"
        self.db.flush()
        self.assertEqual(vote.shortlisted_at.replace(tzinfo=timezone.utc), first)

        self.assertTrue(ensure_user_shortlisted(
            self.db, opportunity=self.opportunity, user=self.user, now=third
        ))
        self.assertEqual(vote.shortlisted_at.replace(tzinfo=timezone.utc), third)

    def test_cast_vote_reaffirmation_preserves_and_leaving_retains_timestamp(self):
        cast_vote(
            self.db, org_id=self.org.id, user_id=self.user.id,
            opp_id=self.opportunity.id, vote="PURSUE", toggle_existing=False,
        )
        vote = self.db.query(Vote).one()
        initial = vote.shortlisted_at
        self.assertIsNotNone(initial)

        cast_vote(
            self.db, org_id=self.org.id, user_id=self.user.id,
            opp_id=self.opportunity.id, vote="PURSUE", toggle_existing=False,
        )
        self.db.refresh(vote)
        self.assertEqual(vote.shortlisted_at, initial)

        cast_vote(
            self.db, org_id=self.org.id, user_id=self.user.id,
            opp_id=self.opportunity.id, vote="PASS",
        )
        self.db.refresh(vote)
        self.assertEqual(vote.shortlisted_at, initial)


class SortingTemplateTests(unittest.TestCase):
    def test_shared_toolbar_and_shortlist_enable_expected_options(self):
        with open("src/bidlens/templates/_queue_layout.html", encoding="utf-8") as handle:
            toolbar = handle.read()
        with open("src/bidlens/templates/my_shortlist.html", encoding="utf-8") as handle:
            shortlist = handle.read()
        self.assertIn(">Imported</option>", toolbar)
        self.assertIn(">Lane</option>", toolbar)
        self.assertIn(">Date Shortlisted</option>", toolbar)
        self.assertIn("shortlist_sort_options=true", shortlist)

    def test_selected_options_render_for_public_query_values(self):
        macro = opportunity_routes.templates.env.get_template("_queue_layout.html").module.queue_toolbar
        lane_html = str(macro(
            "/", "feed", "lane", "asc", "", show_filters=False,
            feed_sort_options=True,
        ))
        shortlist_html = str(macro(
            "/my-shortlist", "shortlist", "shortlisted", "desc", "",
            show_filters=False, feed_sort_options=True, shortlist_sort_options=True,
        ))
        self.assertIn('value="lane" selected', lane_html)
        self.assertIn('value="shortlisted" selected', shortlist_html)


class SortingRouteFlowTests(unittest.TestCase):
    def _assert_route_sort(self, route, *, requested_sort, expected_sort, admin=False):
        user = SimpleNamespace(
            id=7,
            organization_id=11,
            current_organization_id=11,
            current_role="admin" if admin else "member",
            triage_enabled=True,
        )
        query = MagicMock()
        query.outerjoin.return_value = query
        query.join.return_value = query
        query.filter.return_value = query
        query.order_by.return_value = query
        query.count.return_value = 0
        query.offset.return_value.limit.return_value.all.return_value = []
        query.limit.return_value.all.return_value = []
        db = MagicMock()
        db.query.return_value = query
        captured = {}

        def apply_ordering(current_query, **kwargs):
            captured.update(kwargs)
            return current_query

        def render(template_name, context):
            captured["template"] = template_name
            captured["context"] = context
            return MagicMock()

        with (
            patch.object(opportunity_routes, "require_user", return_value=user),
            patch.object(opportunity_routes, "_pre_live_product_redirect", return_value=None),
            patch.object(opportunity_routes, "feed_awaiting_review_query", return_value=query),
            patch.object(opportunity_routes, "_my_shortlist_query", return_value=query),
            patch.object(opportunity_routes, "_user_archive_query", return_value=query),
            patch.object(opportunity_routes, "_apply_feed_search", side_effect=lambda current, **kwargs: current),
            patch.object(opportunity_routes, "_apply_lane_filter", side_effect=lambda current, *args, **kwargs: current),
            patch.object(opportunity_routes, "_apply_stage_filter", side_effect=lambda current, *args, **kwargs: current),
            patch.object(opportunity_routes, "_apply_triage_source_filter", side_effect=lambda current, *args, **kwargs: current),
            patch.object(opportunity_routes, "_exclude_inactive_govwin_stages", side_effect=lambda current: current),
            patch.object(opportunity_routes, "_apply_feed_ordering", side_effect=apply_ordering),
            patch.object(opportunity_routes, "_enrich_opps", return_value=[]),
            patch.object(opportunity_routes, "get_sidebar", return_value={}),
            patch.object(opportunity_routes, "_active_lanes", return_value=[]),
            patch.object(opportunity_routes, "user_my_lanes", return_value=[]),
            patch.object(opportunity_routes.templates, "TemplateResponse", side_effect=render),
        ):
            asyncio.run(route(request=MagicMock(), sort=requested_sort, db=db))

        self.assertEqual(captured["sort"], expected_sort)
        self.assertEqual(captured["context"]["sort"], expected_sort)
        return captured

    def test_lane_survives_feed_route(self):
        self._assert_route_sort(opportunity_routes.feed, requested_sort="lane", expected_sort="lane")

    def test_lane_survives_my_shortlist_route(self):
        captured = self._assert_route_sort(
            opportunity_routes.my_shortlist, requested_sort="lane", expected_sort="lane"
        )
        self.assertTrue(captured["allow_shortlisted"])

    def test_date_shortlisted_survives_my_shortlist_route(self):
        captured = self._assert_route_sort(
            opportunity_routes.my_shortlist,
            requested_sort="shortlisted",
            expected_sort="shortlisted",
        )
        self.assertTrue(captured["allow_shortlisted"])

    def test_lane_survives_triage_route(self):
        self._assert_route_sort(
            opportunity_routes.triage_queue,
            requested_sort="lane",
            expected_sort="lane",
            admin=True,
        )

    def test_lane_survives_archive_route(self):
        self._assert_route_sort(opportunity_routes.archive, requested_sort="lane", expected_sort="lane")

    def test_invalid_sort_falls_back_in_route_and_template_context(self):
        self._assert_route_sort(
            opportunity_routes.feed, requested_sort="not-a-sort", expected_sort="imported"
        )


if __name__ == "__main__":
    unittest.main()
