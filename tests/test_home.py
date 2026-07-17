import datetime as dt
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import (
    CompanyProfile,
    DailySnapshot,
    Event,
    IngestionRun,
    Opportunity,
    Organization,
    OrganizationMembership,
    OpportunityPursuitLaneMatch,
    PursuitLane,
    SamSourceConfig,
    User,
    Vote,
    Workspace,
    WorkspaceInvitation,
)
from bidlens.routes.home import go_live, home_page, organization_setup_page
from bidlens.routes import opportunities
from bidlens.services.feed_queries import feed_awaiting_review_query
from bidlens.services.home import get_daily_brief_home_context, get_home_context


class HomeContextTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Home Org", slug="home-org")
        self.other_org = Organization(name="Other Org", slug="other-home-org")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.admin = User(email="admin@home.test", organization_id=self.org.id)
        self.db.add(self.admin)
        self.db.flush()
        self.db.add(OrganizationMembership(
            organization_id=self.org.id,
            user_id=self.admin.id,
            role="admin",
        ))
        self.db.commit()
        self.now = dt.datetime(2026, 7, 6, 16, 0, tzinfo=dt.timezone.utc)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _context(self, *, salesforce_connected=False):
        return get_home_context(
            self.db,
            self.org.id,
            self.admin.id,
            now=self.now,
            salesforce_connected=salesforce_connected,
        )

    def _profile(self):
        self.db.add(CompanyProfile(
            org_id=self.org.id,
            company_name="Home Org",
            profile_json={"company_overview": "Public-sector research"},
        ))
        self.db.commit()

    def _source(self):
        self.db.add(SamSourceConfig(
            organization_id=self.org.id,
            name="Primary federal search",
            naics_codes=["541611"],
        ))
        self.db.commit()

    def _opportunity(self, **overrides):
        values = {
            "organization_id": self.org.id,
            "source": "sam.gov",
            "source_record_id": "HOME-1",
            "title": "Home opportunity",
            "agency": "Test Agency",
            "opportunity_type": "Solicitation",
            "posted_date": dt.date(2026, 7, 1),
            "response_deadline": dt.date.today() + dt.timedelta(days=30),
            "qualification_status": "unreviewed",
            "decision_state": "INBOX",
        }
        values.update(overrides)
        opportunity = Opportunity(**values)
        self.db.add(opportunity)
        self.db.commit()
        return opportunity

    def test_empty_workspace_shows_only_applicable_setup_actions(self):
        context = self._context()
        steps = {item["key"]: item for item in context["next_steps"]}

        self.assertEqual(
            list(steps),
            ["company-profile", "opportunity-source", "invite-team", "business-systems", "feed-rules", "pursuit-lanes"],
        )
        self.assertEqual(steps["company-profile"]["label"], "Required")
        self.assertEqual(steps["business-systems"]["label"], "Optional")
        self.assertEqual(steps["opportunity-source"]["cta_url"], f"/opportunity-discovery?org_id={self.org.id}")
        self.assertEqual(steps["business-systems"]["cta_url"], f"/outbound-integrations?org_id={self.org.id}")
        self.assertEqual(steps["feed-rules"]["cta_url"], f"/settings?org_id={self.org.id}")
        self.assertNotIn("first-import", steps)
        self.assertNotIn("first-review", steps)
        self.assertFalse(context["workspace_summary"]["required_setup_complete"])
        self.assertFalse(context["is_live"])
        self.assertFalse(context["can_go_live"])
        self.assertIsNone(context["operational_home_context"])
        self.assertEqual(context["workspace_summary"]["headline"], "Welcome to BidLens.")
        self.assertIn("Organization created", [item["title"] for item in context["completed"]])
        self.assertEqual(steps["company-profile"]["title"], "Configure Organization")
        self.assertEqual(steps["opportunity-source"]["title"], "Enable Opportunity Discovery")
        self.assertEqual(steps["business-systems"]["title"], "Connect Business Systems")

    def test_configured_source_without_opportunities_completes_required_setup(self):
        self._profile()
        self._source()

        context = self._context()
        steps = {item["key"]: item for item in context["next_steps"]}

        self.assertNotIn("first-import", steps)
        self.assertNotIn("opportunity-source", steps)
        self.assertEqual(context["operational_snapshot"]["sources_enabled"], 1)
        self.assertTrue(context["workspace_summary"]["required_setup_complete"])
        self.assertTrue(context["can_go_live"])
        self.assertIsNone(context["operational_home_context"])
        self.assertIn("Opportunity Discovery enabled", [item["title"] for item in context["completed"]])

    def test_pending_invitation_completes_users_setup_task_and_remains_editable(self):
        workspace = Workspace(organization_id=self.org.id, name="Home Workspace", slug="home-workspace")
        self.db.add(workspace)
        self.db.flush()
        self.db.add(WorkspaceInvitation(
            organization_id=self.org.id,
            workspace_id=workspace.id,
            email="pending@home.test",
            role="member",
            status="pending",
            token="pending-token",
        ))
        self.db.commit()

        context = self._context()
        steps = {item["key"]: item for item in context["next_steps"]}
        completed = {item["key"]: item for item in context["completed"]}

        self.assertNotIn("invite-team", steps)
        self.assertIn("invite-team", completed)
        self.assertEqual(
            completed["invite-team"]["cta_url"],
            f"/admin/organizations/{self.org.id}/users?org_id={self.org.id}",
        )
        self.assertIn("pending", completed["invite-team"]["description"].lower())

    def test_required_completion_makes_pre_live_workspace_ready_to_go_live(self):
        self._profile()
        self._source()
        self._opportunity()
        self.db.add(IngestionRun(
            source="sam.gov",
            organization_id=self.org.id,
            user_id=self.admin.id,
            started_at=dt.datetime(2026, 7, 6, 14, 0),
            finished_at=dt.datetime(2026, 7, 6, 14, 5),
            status="completed",
            error_count=0,
        ))
        self.db.commit()

        context = self._context()
        steps = {item["key"]: item for item in context["next_steps"]}

        self.assertTrue(context["workspace_summary"]["required_setup_complete"])
        self.assertFalse(context["is_live"])
        self.assertTrue(context["can_go_live"])
        self.assertEqual(context["workspace_summary"]["description"], "Let’s get your organization ready.")
        self.assertNotIn("first-review", steps)
        self.assertNotIn("first-import", steps)
        self.assertEqual(context["operational_snapshot"]["opportunities_awaiting_review"], 1)
        self.assertIsNotNone(context["operational_snapshot"]["last_successful_import"])
        self.assertIsNone(context["operational_home_context"])

    def test_live_workspace_returns_operational_home_context(self):
        self._profile()
        self._source()
        self._opportunity()
        self.org.is_live = True
        self.db.commit()

        context = self._context()

        self.assertTrue(context["is_live"])
        self.assertFalse(context["can_go_live"])
        self.assertEqual(context["workspace_summary"]["description"], "Your workspace is ready.")
        self.assertIsNotNone(context["operational_home_context"])
        self.assertEqual(
            context["operational_home_context"]["operational_snapshot"]["opportunities_awaiting_review"],
            1,
        )

    def test_home_context_loads_stored_daily_snapshot_for_today(self):
        workspace = Workspace(
            organization_id=self.org.id,
            name="Home Workspace",
            slug="home-workspace",
        )
        self.db.add(workspace)
        self.db.flush()
        self.db.add(DailySnapshot(
            workspace_id=workspace.id,
            user_id=self.admin.id,
            snapshot_date=self.now.date(),
            status="completed",
            snapshot_json={
                "new_opportunities": [{"title": "Stored new opportunity", "agency": "Agency"}],
                "updated_opportunities": [],
                "upcoming_deadlines": [],
                "interested_activity": [],
                "team_activity": [],
                "shortlist_changes": [],
            },
        ))
        self.db.commit()

        context = self._context()

        self.assertIsNotNone(context["daily_snapshot"])
        self.assertEqual(context["daily_snapshot"]["snapshot_date"], self.now.date())
        self.assertEqual(
            context["daily_snapshot"]["sections"]["new_opportunities"][0]["title"],
            "Stored new opportunity",
        )

    def test_daily_brief_context_renders_stored_v1_sections(self):
        self._opportunity(
            source_record_id="LIVE-FEED-1",
            title="Live feed opportunity one",
            qualification_status="qualified",
            decision_state="INBOX",
            response_deadline=dt.date.today() + dt.timedelta(days=30),
        )
        self._opportunity(
            source_record_id="LIVE-FEED-2",
            title="Live feed opportunity two",
            qualification_status="qualified",
            decision_state="INBOX",
            response_deadline=dt.date.today() + dt.timedelta(days=31),
        )
        workspace = Workspace(
            organization_id=self.org.id,
            name="Home Workspace",
            slug="home-workspace",
        )
        self.db.add(workspace)
        self.db.flush()
        self.db.add(DailySnapshot(
            workspace_id=workspace.id,
            user_id=self.admin.id,
            snapshot_date=self.now.date(),
            status="completed",
            snapshot_json={
                "summary": {
                    "new_feed_count": 52,
                    "shortlist_update_count": 1,
                    "team_signal_count": 0,
                    "shortlist_deadline_count": 0,
                    "connector_issue_count": 0,
                },
                "shortlist_updates": [
                    {
                        "title": "NIH Survey Support",
                        "subtitle": "Amendment 2 posted",
                        "destination_url": "/opportunity/123",
                    }
                ],
                "team_signals": [],
                "shortlist_deadlines": [],
                "new_opportunities": [
                    {
                        "id": 321,
                        "title": "Stored new opportunity",
                        "agency": "Stored Agency",
                        "response_deadline": "2026-07-12",
                    }
                ],
            },
        ))
        self.db.commit()

        context = get_daily_brief_home_context(
            self.db,
            self.org.id,
            self.admin.id,
            now=self.now,
        )

        self.assertTrue(context["has_updates"])
        self.assertFalse(context["snapshot_missing"])
        self.assertEqual(
            context["brief_points"],
            [
                "1 opportunity on your Shortlist changed.",
            ],
        )
        self.assertEqual(context["feed_review"]["count"], 2)
        self.assertEqual(context["feed_review"]["message"], "2 opportunities awaiting review.")
        self.assertEqual(context["feed_review"]["action_label"], "Review Feed")
        self.assertEqual([section["key"] for section in context["sections"]], ["shortlist_updates"])
        self.assertEqual(context["sections"][0]["count"], 1)
        self.assertEqual(context["sections"][0]["items"][0]["destination_url"], "/opportunity/123")
        self.assertEqual(context["shortlist_sections"], [])
        self.assertEqual(context["actions"], [])

    def test_daily_brief_feed_count_updates_without_regenerating_snapshot(self):
        first = self._opportunity(
            source_record_id="LIVE-REVIEW-1",
            title="First live opportunity",
            qualification_status="qualified",
            decision_state="INBOX",
            response_deadline=dt.date.today() + dt.timedelta(days=30),
        )
        second = self._opportunity(
            source_record_id="LIVE-REVIEW-2",
            title="Second live opportunity",
            qualification_status="qualified",
            decision_state="INBOX",
            response_deadline=dt.date.today() + dt.timedelta(days=31),
        )
        workspace = Workspace(
            organization_id=self.org.id,
            name="Home Workspace",
            slug="home-workspace-live-overlay",
        )
        self.db.add(workspace)
        self.db.flush()
        snapshot = DailySnapshot(
            workspace_id=workspace.id,
            user_id=self.admin.id,
            snapshot_date=self.now.date(),
            status="completed",
            snapshot_json={
                "summary": {
                    "new_feed_count": 99,
                    "shortlist_update_count": 1,
                    "team_signal_count": 0,
                    "shortlist_deadline_count": 0,
                    "connector_issue_count": 0,
                },
                "shortlist_updates": [
                    {
                        "title": "Stored shortlist item",
                        "subtitle": "Amendment posted",
                        "destination_url": "/opportunity/777",
                    }
                ],
                "team_signals": [],
                "shortlist_deadlines": [],
                "connector_issues": [],
            },
        )
        self.db.add(snapshot)
        self.db.commit()

        before = get_daily_brief_home_context(
            self.db,
            self.org.id,
            self.admin.id,
            now=self.now,
        )

        self.db.add(Vote(
            org_id=self.org.id,
            opp_id=first.id,
            user_id=self.admin.id,
            vote="PASS",
        ))
        self.db.commit()
        after_one_review = get_daily_brief_home_context(
            self.db,
            self.org.id,
            self.admin.id,
            now=self.now,
        )

        self.db.add(Vote(
            org_id=self.org.id,
            opp_id=second.id,
            user_id=self.admin.id,
            vote="PURSUE",
        ))
        self.db.commit()
        after_cleared = get_daily_brief_home_context(
            self.db,
            self.org.id,
            self.admin.id,
            now=self.now,
        )

        self.assertEqual(before["snapshot"]["id"], snapshot.id)
        self.assertEqual(after_one_review["snapshot"]["id"], snapshot.id)
        self.assertEqual(after_cleared["snapshot"]["id"], snapshot.id)
        self.assertEqual(before["feed_review"]["count"], 2)
        self.assertEqual(after_one_review["feed_review"]["count"], 1)
        self.assertEqual(after_cleared["feed_review"]["count"], 0)
        self.assertTrue(after_cleared["feed_review"]["complete"])
        self.assertEqual(after_cleared["feed_review"]["message"], "No opportunities awaiting review.")
        self.assertEqual(before["brief_points"], after_one_review["brief_points"])
        self.assertEqual(before["brief_points"], after_cleared["brief_points"])
        self.assertEqual(before["sections"], after_one_review["sections"])
        self.assertEqual(before["sections"], after_cleared["sections"])

    def test_daily_brief_feed_count_matches_default_feed_eligibility(self):
        today = dt.date.today()
        included_future = self._opportunity(
            source_record_id="COUNT-FUTURE",
            title="Future opportunity",
            qualification_status="qualified",
            decision_state="INBOX",
            response_deadline=today + dt.timedelta(days=14),
        )
        included_today = self._opportunity(
            source_record_id="COUNT-TODAY",
            title="Due today opportunity",
            qualification_status="qualified",
            decision_state="INBOX",
            response_deadline=today,
        )
        self._opportunity(
            source_record_id="COUNT-PAST",
            title="Past due opportunity",
            qualification_status="qualified",
            decision_state="INBOX",
            response_deadline=today - dt.timedelta(days=1),
        )
        pursued = self._opportunity(
            source_record_id="COUNT-PURSUE",
            title="Pursued opportunity",
            qualification_status="qualified",
            decision_state="INBOX",
            response_deadline=today + dt.timedelta(days=14),
        )
        passed = self._opportunity(
            source_record_id="COUNT-PASS",
            title="Passed opportunity",
            qualification_status="qualified",
            decision_state="INBOX",
            response_deadline=today + dt.timedelta(days=14),
        )
        self._opportunity(
            source_record_id="COUNT-ARCHIVED",
            title="Archived opportunity",
            qualification_status="qualified",
            decision_state="ARCHIVED",
            response_deadline=today + dt.timedelta(days=14),
        )
        self._opportunity(
            source_record_id="COUNT-UNQUALIFIED",
            title="Unqualified opportunity",
            qualification_status="rejected",
            decision_state="INBOX",
            response_deadline=today + dt.timedelta(days=14),
        )
        self._opportunity(
            source="govwin_export",
            source_record_id="COUNT-GOVWIN-SOURCE-SELECTION",
            title="Inactive GovWin opportunity",
            opportunity_type="Source Selection",
            source_stage="Source Selection",
            qualification_status="qualified",
            decision_state="INBOX",
            response_deadline=today + dt.timedelta(days=14),
        )
        workspace = Workspace(
            organization_id=self.org.id,
            name="Home Workspace",
            slug="home-workspace-feed-count",
        )
        self.db.add(workspace)
        self.db.flush()
        self.db.add_all([
            Vote(org_id=self.org.id, user_id=self.admin.id, opp_id=pursued.id, vote="PURSUE"),
            Vote(org_id=self.org.id, user_id=self.admin.id, opp_id=passed.id, vote="PASS"),
            DailySnapshot(
                workspace_id=workspace.id,
                user_id=self.admin.id,
                snapshot_date=self.now.date(),
                status="completed",
                snapshot_json={
                    "summary": {},
                    "shortlist_updates": [],
                    "team_signals": [],
                    "shortlist_deadlines": [],
                    "connector_issues": [],
                },
            ),
        ])
        self.db.commit()

        default_feed_query = feed_awaiting_review_query(
            self.db,
            organization_id=self.org.id,
            user_id=self.admin.id,
        )
        default_feed_ids = {opportunity.id for opportunity, _watched in default_feed_query.all()}
        context = get_daily_brief_home_context(
            self.db,
            self.org.id,
            self.admin.id,
            now=self.now,
        )

        self.assertEqual(default_feed_ids, {included_future.id, included_today.id})
        self.assertEqual(context["feed_review"]["count"], len(default_feed_ids))
        self.assertEqual(context["feed_review"]["message"], "2 opportunities awaiting review.")

    def test_daily_brief_feed_review_includes_top_lane_breakdown(self):
        today = dt.date.today()
        lanes = [
            PursuitLane(organization_id=self.org.id, name="Health"),
            PursuitLane(organization_id=self.org.id, name="Education"),
            PursuitLane(organization_id=self.org.id, name="Transportation"),
            PursuitLane(organization_id=self.org.id, name="Infrastructure"),
        ]
        self.db.add_all(lanes)
        self.db.flush()
        opportunities = [
            self._opportunity(
                source_record_id=f"LANE-BREAKDOWN-{index}",
                title=f"Lane opportunity {index}",
                qualification_status="qualified",
                decision_state="INBOX",
                response_deadline=today + dt.timedelta(days=14),
            )
            for index in range(7)
        ]
        past_due = self._opportunity(
            source_record_id="LANE-BREAKDOWN-PAST",
            title="Past due lane opportunity",
            qualification_status="qualified",
            decision_state="INBOX",
            response_deadline=today - dt.timedelta(days=1),
        )
        workspace = Workspace(
            organization_id=self.org.id,
            name="Home Workspace",
            slug="home-workspace-lane-breakdown",
        )
        self.db.add(workspace)
        self.db.flush()
        matches = []
        for opportunity in opportunities[:3]:
            matches.append(OpportunityPursuitLaneMatch(
                organization_id=self.org.id,
                opportunity_id=opportunity.id,
                pursuit_lane_id=lanes[0].id,
            ))
        for opportunity in opportunities[3:5]:
            matches.append(OpportunityPursuitLaneMatch(
                organization_id=self.org.id,
                opportunity_id=opportunity.id,
                pursuit_lane_id=lanes[1].id,
            ))
        matches.extend([
            OpportunityPursuitLaneMatch(
                organization_id=self.org.id,
                opportunity_id=opportunities[5].id,
                pursuit_lane_id=lanes[2].id,
            ),
            OpportunityPursuitLaneMatch(
                organization_id=self.org.id,
                opportunity_id=opportunities[6].id,
                pursuit_lane_id=lanes[3].id,
            ),
            OpportunityPursuitLaneMatch(
                organization_id=self.org.id,
                opportunity_id=past_due.id,
                pursuit_lane_id=lanes[0].id,
            ),
        ])
        self.db.add_all(matches)
        self.db.add(DailySnapshot(
            workspace_id=workspace.id,
            user_id=self.admin.id,
            snapshot_date=self.now.date(),
            status="completed",
            snapshot_json={"summary": {}},
        ))
        self.db.commit()

        context = get_daily_brief_home_context(
            self.db,
            self.org.id,
            self.admin.id,
            now=self.now,
        )

        self.assertEqual(context["feed_review"]["count"], 7)
        self.assertEqual(
            context["feed_review"]["lanes"],
            [
                {"id": lanes[0].id, "name": "Health", "count": 3},
                {"id": lanes[1].id, "name": "Education", "count": 2},
                {"id": lanes[3].id, "name": "Infrastructure", "count": 1},
            ],
        )
        self.assertEqual(context["feed_review"]["more_lane_count"], 1)

    def test_daily_brief_context_distinguishes_missing_snapshot_from_no_activity(self):
        self._opportunity(
            title="Database activity should not render without a stored snapshot",
            created_at=self.now,
        )

        context = get_daily_brief_home_context(
            self.db,
            self.org.id,
            self.admin.id,
            now=self.now,
        )

        self.assertTrue(context["snapshot_missing"])
        self.assertFalse(context["has_updates"])
        self.assertEqual(context["sections"], [])
        self.assertEqual(context["shortlist_sections"], [])

    def test_daily_brief_context_empty_snapshot_has_no_activity_state(self):
        workspace = Workspace(
            organization_id=self.org.id,
            name="Home Workspace",
            slug="home-workspace-empty",
        )
        self.db.add(workspace)
        self.db.flush()
        self.db.add(DailySnapshot(
            workspace_id=workspace.id,
            user_id=self.admin.id,
            snapshot_date=self.now.date(),
            status="completed",
            snapshot_json={
                "snapshot_date": self.now.date().isoformat(),
                "activity_date": "2026-07-05",
                "new_opportunities": [],
                "updated_opportunities": [],
                "upcoming_deadlines": [],
                "interested_activity": [],
                "shortlist_changes": [],
                "connector_issues": [],
            },
        ))
        self.db.commit()

        context = get_daily_brief_home_context(
            self.db,
            self.org.id,
            self.admin.id,
            now=self.now,
        )

        self.assertFalse(context["snapshot_missing"])
        self.assertTrue(context["has_updates"])
        self.assertEqual(context["brief_points"], [])
        self.assertEqual(context["feed_review"]["count"], 0)
        self.assertTrue(context["feed_review"]["complete"])
        self.assertEqual(context["feed_review"]["message"], "No opportunities awaiting review.")
        self.assertEqual(context["sections"], [])

    def test_home_template_renders_daily_brief_section_items(self):
        environment = Environment(
            loader=FileSystemLoader("src/bidlens/templates"),
            autoescape=select_autoescape(["html"]),
        )
        template = environment.get_template("home.html")

        rendered = template.render(
            request=SimpleNamespace(),
            user=SimpleNamespace(name="Home User", email="home-user@test.local"),
            active_page="home",
            home={
                "snapshot_date": self.now.date(),
                "snapshot_missing": False,
                "has_updates": True,
                "brief_points": ["1 opportunity on your Shortlist changed."],
                "feed_review": {
                    "count": 1,
                    "message": "1 opportunity awaiting review.",
                    "complete": False,
                    "url": "/",
                    "action_label": "Review Feed",
                    "lanes": [
                        {"id": 1, "name": "Health", "count": 5},
                        {"id": 2, "name": "Education", "count": 2},
                    ],
                    "more_lane_count": 1,
                },
                "actions": [],
                "sections": [
                    {
                        "key": "shortlist_updates",
                        "title": "Shortlist Changes",
                        "count": 1,
                        "items": [
                            {
                                "title": "Stored opportunity",
                                "subtitle": "Stored Agency",
                                "destination_url": "/opportunity/321",
                            }
                        ],
                    },
                    {
                        "key": "shortlist_deadlines",
                        "title": "Upcoming Due Dates",
                        "count": 1,
                        "action_label": "Review Shortlist",
                        "action_url": "/my-shortlist",
                        "items": [
                            {
                                "title": "Due opportunity",
                                "subtitle": "Due tomorrow",
                                "destination_url": "/opportunity/654",
                            }
                        ],
                    },
                    {
                        "key": "team_signals",
                        "title": "Activity",
                        "count": 1,
                        "items": [
                            {
                                "title": "Teammate activity",
                                "subtitle": "A teammate joined",
                                "destination_url": "/opportunity/987",
                            }
                        ],
                    }
                ],
                "shortlist_sections": [
                    {
                        "key": "shortlist_deadlines",
                        "title": "Upcoming Due Dates",
                        "count": 1,
                        "items": [
                            {
                                "title": "Due opportunity",
                                "subtitle": "Due tomorrow",
                                "destination_url": "/opportunity/654",
                            }
                        ],
                    },
                    {
                        "key": "team_signals",
                        "title": "Activity",
                        "count": 1,
                        "items": [
                            {
                                "title": "Teammate activity",
                                "subtitle": "A teammate joined",
                                "destination_url": "/opportunity/987",
                            }
                        ],
                    },
                ],
            },
        )

        self.assertIn("1 opportunity awaiting review.", rendered)
        self.assertIn("Home's Daily Brief", rendered)
        self.assertIn("Health", rendered)
        self.assertIn("Education", rendered)
        self.assertIn("+1", rendered)
        self.assertIn("pursuit-lane-pill", rendered)
        self.assertIn("Review Feed", rendered)
        self.assertIn("Review Shortlist", rendered)
        self.assertEqual(rendered.count("Review Feed"), 1)
        self.assertEqual(rendered.count("Review Shortlist"), 1)
        self.assertIn("Shortlist", rendered)
        self.assertIn("<details class=\"home-brief-detail-section\" open>", rendered)
        self.assertIn("/opportunity/654", rendered)
        self.assertIn("/opportunity/987", rendered)
        self.assertIn("Daily Brief", rendered)
        self.assertIn("Upcoming Due Dates", rendered)
        self.assertIn("Activity", rendered)
        self.assertNotIn(">Yesterday<", rendered)
        self.assertNotIn("Shortlist Deadlines", rendered)
        self.assertNotIn("Team Signals", rendered)

    def test_home_page_is_available_to_members(self):
        member = User(email="member-home@test.local", organization_id=self.org.id)
        self.db.add(member)
        self.db.flush()
        self.db.add(OrganizationMembership(
            organization_id=self.org.id,
            user_id=member.id,
            role="member",
        ))
        self.db.commit()
        setattr(member, "current_organization_id", self.org.id)
        setattr(member, "current_role", "member")

        with (
            patch("bidlens.routes.home.get_current_user", return_value=member),
            patch("bidlens.routes.home.attach_request_user_context", return_value=member),
            patch("bidlens.routes.home.templates.TemplateResponse", return_value={"ok": True}) as template_response,
        ):
            response = asyncio.run(home_page(SimpleNamespace(), self.db))

        self.assertEqual(response, {"ok": True})
        template_response.assert_called_once()
        self.assertEqual(template_response.call_args.args[0], "home.html")

    def test_home_page_redirects_pre_live_admin_to_organization_setup(self):
        setattr(self.admin, "current_organization_id", self.org.id)
        setattr(self.admin, "current_role", "admin")
        setattr(self.admin, "current_organization_is_live", False)

        with (
            patch("bidlens.routes.home.get_current_user", return_value=self.admin),
            patch("bidlens.routes.home.attach_request_user_context", return_value=self.admin),
        ):
            response = asyncio.run(home_page(SimpleNamespace(), self.db))

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/organization-setup?org_id={self.org.id}")

    def test_feed_redirects_pre_live_admin_to_organization_setup(self):
        setattr(self.admin, "current_organization_id", self.org.id)
        setattr(self.admin, "current_role", "admin")
        setattr(self.admin, "current_organization_is_live", False)

        with patch("bidlens.routes.opportunities.require_user", return_value=self.admin):
            response = asyncio.run(opportunities.feed(SimpleNamespace(), db=self.db))

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/organization-setup?org_id={self.org.id}")

    def test_organization_setup_page_renders_pre_live_admin_checklist(self):
        setattr(self.admin, "current_organization_id", self.org.id)
        setattr(self.admin, "current_role", "admin")

        with (
            patch("bidlens.routes.home.get_current_user", return_value=self.admin),
            patch("bidlens.routes.home.attach_request_user_context", return_value=self.admin),
            patch("bidlens.routes.home.templates.TemplateResponse", return_value={"ok": True}) as template_response,
        ):
            response = asyncio.run(organization_setup_page(SimpleNamespace(), self.db))

        self.assertEqual(response, {"ok": True})
        template_response.assert_called_once()
        self.assertEqual(template_response.call_args.args[0], "organization_setup.html")
        context = template_response.call_args.args[1]
        self.assertFalse(context["home"]["is_live"])
        self.assertIn("next_steps", context["home"])

    def test_organization_setup_page_redirects_live_workspace_to_home(self):
        self.org.is_live = True
        self.db.commit()
        setattr(self.admin, "current_organization_id", self.org.id)
        setattr(self.admin, "current_role", "admin")

        with (
            patch("bidlens.routes.home.get_current_user", return_value=self.admin),
            patch("bidlens.routes.home.attach_request_user_context", return_value=self.admin),
        ):
            response = asyncio.run(organization_setup_page(SimpleNamespace(), self.db))

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/home?org_id={self.org.id}")

    def test_go_live_route_sets_org_live_and_records_event(self):
        self._profile()
        self._source()
        setattr(self.admin, "current_organization_id", self.org.id)
        setattr(self.admin, "current_role", "admin")

        with (
            patch("bidlens.routes.home.get_current_user", return_value=self.admin),
            patch("bidlens.routes.home.attach_request_user_context", return_value=self.admin),
        ):
            response = asyncio.run(go_live(SimpleNamespace(), self.db))

        self.db.refresh(self.org)
        event = (
            self.db.query(Event)
            .filter(Event.org_id == self.org.id, Event.event_type == "workspace_went_live")
            .first()
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/home")
        self.assertTrue(self.org.is_live)
        self.assertIsNotNone(event)

    def test_completed_recommendations_disappear_and_salesforce_remains_optional(self):
        self._profile()
        self._source()
        self._opportunity(decision_state="SHORTLISTED", qualification_status="qualified")
        second_user = User(email="member@home.test", organization_id=self.org.id)
        self.db.add(second_user)
        self.db.flush()
        self.db.add_all([
            OrganizationMembership(
                organization_id=self.org.id,
                user_id=second_user.id,
                role="member",
            ),
            Event(
                org_id=self.org.id,
                user_id=self.admin.id,
                opp_id=None,
                event_type="feed_rules_configured",
                payload={"source": "test"},
            ),
            PursuitLane(
                organization_id=self.org.id,
                name="Health",
                agencies=[],
                naics=[],
                keywords=[],
                set_asides=[],
            ),
        ])
        self.db.commit()

        disconnected = self._context(salesforce_connected=False)
        connected = self._context(salesforce_connected=True)

        self.assertEqual([step["key"] for step in disconnected["next_steps"]], ["business-systems"])
        self.assertTrue(disconnected["workspace_summary"]["required_setup_complete"])
        self.assertEqual(connected["next_steps"], [])
        self.assertTrue(connected["operational_snapshot"]["salesforce_connected"])

    def test_latest_failed_connector_run_creates_attention_item(self):
        self._profile()
        self._source()
        self._opportunity(decision_state="ARCHIVED")
        self.db.add(IngestionRun(
            source="sam.gov",
            organization_id=self.org.id,
            user_id=self.admin.id,
            started_at=dt.datetime(2026, 7, 6, 15, 0),
            finished_at=dt.datetime(2026, 7, 6, 15, 1),
            status="failed",
            error_count=1,
            notes="SAM.gov returned an authentication error.",
        ))
        self.db.commit()

        context = self._context()

        self.assertEqual(context["operational_snapshot"]["connector_issues"], 1)
        self.assertEqual(len(context["attention_items"]), 1)
        self.assertIn("authentication error", context["attention_items"][0]["description"])

    def test_other_organization_records_do_not_change_workspace_state(self):
        self.db.add_all([
            CompanyProfile(
                org_id=self.other_org.id,
                company_name="Other Org",
                profile_json={"company_overview": "Other"},
            ),
            SamSourceConfig(
                organization_id=self.other_org.id,
                name="Other search",
                naics_codes=["999999"],
            ),
        ])
        self.db.commit()

        context = self._context()
        step_keys = {item["key"] for item in context["next_steps"]}

        self.assertIn("company-profile", step_keys)
        self.assertIn("opportunity-source", step_keys)
        self.assertEqual(context["operational_snapshot"]["sources_enabled"], 0)

    def test_grants_enabled_event_counts_as_opportunity_discovery(self):
        self._profile()
        self.db.add(Event(
            org_id=self.org.id,
            user_id=self.admin.id,
            opp_id=None,
            event_type="opportunity_source_enabled",
            payload={"source": "grants.gov"},
        ))
        self.db.commit()

        context = self._context()
        step_keys = {item["key"] for item in context["next_steps"]}

        self.assertNotIn("opportunity-source", step_keys)
        self.assertTrue(context["workspace_summary"]["required_setup_complete"])
        self.assertEqual(context["operational_snapshot"]["sources_enabled"], 1)

    def test_feed_rules_are_recommended_until_configured(self):
        self._profile()
        self._source()

        before = self._context()
        self.db.add(Event(
            org_id=self.org.id,
            user_id=self.admin.id,
            opp_id=None,
            event_type="feed_rules_configured",
            payload={"source": "test"},
        ))
        self.db.commit()
        after = self._context()

        self.assertIn("feed-rules", [step["key"] for step in before["next_steps"]])
        self.assertNotIn("feed-rules", [step["key"] for step in after["next_steps"]])
        self.assertIn("Feed rules configured", [item["title"] for item in after["completed"]])


if __name__ == "__main__":
    unittest.main()
