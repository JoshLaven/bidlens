import asyncio
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import Opportunity, Organization, OrganizationMembership, OrgProfile, PursuitLane, User
from bidlens.routes import pursuit_lanes, settings
from bidlens.services.pursuit_lanes import lane_match_terms, match_lane_to_opportunity


class FeedSettingsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Feed Settings Org", slug="feed-settings-org", is_live=True)
        self.other_org = Organization(name="Other Feed Org", slug="other-feed-org")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.admin = User(email="admin@feed.test", organization_id=self.org.id)
        self.db.add(self.admin)
        self.db.flush()
        self.db.add(OrganizationMembership(
            organization_id=self.org.id,
            user_id=self.admin.id,
            role="admin",
        ))
        self.db.commit()
        setattr(self.admin, "current_organization_id", self.org.id)
        setattr(self.admin, "current_role", "admin")
        setattr(self.admin, "current_organization_is_live", True)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _request(self, path="/settings", headers=None):
        return SimpleNamespace(
            headers=headers or {},
            query_params={"org_id": str(self.org.id)},
            url=SimpleNamespace(path=path, query=f"org_id={self.org.id}"),
        )

    def test_settings_page_contains_workflow_and_tenant_scoped_lane_management(self):
        self.db.add_all([
            PursuitLane(organization_id=self.org.id, name="Healthcare", agencies=[] , naics=[], keywords=[], set_asides=[]),
            PursuitLane(organization_id=self.other_org.id, name="Secret Other Lane", agencies=[], naics=[], keywords=[], set_asides=[]),
        ])
        self.db.commit()
        with (
            patch.object(settings, "require_user", return_value=self.admin),
            patch.object(settings.templates, "TemplateResponse", return_value={"ok": True}) as response,
        ):
            result = asyncio.run(settings.settings_page(self._request(), self.db))

        self.assertEqual(result, {"ok": True})
        self.assertEqual(response.call_args.args[0], "pursuit_lanes.html")
        context = response.call_args.args[1]
        self.assertEqual([lane.name for lane in context["lanes"]], ["Healthcare"])
        self.assertTrue(context["is_admin"])

    def test_feed_settings_template_is_workspace_configuration_only(self):
        template = Path("src/bidlens/templates/pursuit_lanes.html").read_text()
        self.assertIn("Feed Settings", template)
        self.assertIn("workspace_management_hero('Feed Settings'", template)
        self.assertIn("none, action_url=setup_back_url", template)
        self.assertIn("Feed Workflow", template)
        self.assertNotIn("Review Flow", template)
        self.assertNotIn("Use Triage when admins should qualify imported opportunities before they appear in the Feed.", template)
        self.assertNotIn("Choose whether incoming opportunities go directly to the Feed or require admin review first.", template)
        self.assertIn("Route new opportunities through Triage", template)
        self.assertIn(
            "New opportunities must be qualified by an Admin in Triage before appearing in the Feed.",
            template,
        )
        self.assertIn("admin-setting-toggle--switch", template)
        self.assertIn("bidlens-switch", template)
        self.assertIn('onchange="this.form.requestSubmit()"', template)
        self.assertNotIn("Save Feed Workflow", template)
        self.assertIn("Pursuit Lanes", template)
        self.assertNotIn("Configured Lanes", template)
        self.assertIn("+ Add Lane", template)
        self.assertIn("Create Lane", template)
        self.assertIn("lane_editor('/pursuit-lanes'", template)
        self.assertIn("Lane active", template)
        self.assertIn("pursuit-lane-options--create-footer", template)
        self.assertIn("data-lane-summary-title-row", template)
        self.assertIn("badge badge-status-saved", template)
        self.assertIn("pursuit-lane-status-pill--inactive", template)
        self.assertIn("{% if lane.is_active %}", template)
        self.assertIn("data-lane-match-count", template)
        self.assertNotIn("Matching Criteria", template)
        self.assertIn("Match Terms", template)
        self.assertIn("Enter the terms BidLens should use to assign opportunities to this lane. Separate multiple terms with commas.", template)
        self.assertIn("pursuit-lane-chevron", template)
        self.assertIn("data-lane-chevron", template)
        self.assertIn("data-lane-active-toggle", template)
        self.assertIn("onclick=\"event.stopPropagation()\"", template)
        self.assertIn("submitFormJson", template)
        self.assertIn("'X-Requested-With': 'fetch'", template)
        self.assertIn("updateLaneCard", template)
        self.assertIn("laneCardHtml", template)
        self.assertIn("Creating…", template)
        self.assertIn("Saving…", template)
        self.assertIn("Deleting…", template)
        self.assertIn("enterLaneEdit", template)
        self.assertIn("laneObject.dataset.editing = 'true'", template)
        self.assertIn("event.preventDefault()", template)
        self.assertIn("Cancel", template)
        self.assertIn("Save Changes", template)
        self.assertEqual(template.count("Save Changes"), 2)
        self.assertIn("Delete Lane", template)
        self.assertIn("pursuit-lane-delete-action", template)
        self.assertIn("data-lane-delete-form", template)
        self.assertIn("data-inline-error", template)
        self.assertNotIn("pursuit-lane-editor-actions\">\n        <button type=\"button\" class=\"btn btn-outline-secondary\" onclick=\"cancelLaneEdit(this)\">Cancel</button>\n        <button type=\"submit\" class=\"btn btn-primary\" {% if not can_manage_lanes %}disabled{% endif %}>Save Changes</button>", template)
        self.assertIn("confirm('Delete this pursuit lane? This cannot be undone.')", template)
        for legacy_label in ("Description", "Agencies", "NAICS Codes", "Keywords", "Set-Asides"):
            self.assertNotIn(f"<label>{legacy_label}</label>", template)
        self.assertNotIn("My Lanes", template)
        self.assertNotIn("Save My Lanes", template)
        self.assertNotIn('action="/pursuit-lanes/my-lanes', template)
        self.assertNotIn("Feed Eligibility", template)
        self.assertNotIn("Configure Filters", template)
        self.assertNotIn("feed-eligibility-panel", template)
        self.assertNotIn("Rematch Opportunities", template)
        self.assertNotIn('action="/pursuit-lanes/rematch', template)
        self.assertNotIn("New Pursuit Lane", template)
        self.assertNotIn('name="digest_recipients"', template)
        self.assertNotIn('name="digest_max_items"', template)
        self.assertNotIn('name="digest_time_local"', template)

    def test_feed_settings_pursuit_lane_accordion_contract(self):
        template = Path("src/bidlens/templates/pursuit_lanes.html").read_text()
        css = Path("src/bidlens/static/css/styles.css").read_text()

        self.assertIn(".pursuit-lane-summary {\n    position: relative;\n    display: grid;", css)
        self.assertIn("grid-template-columns: minmax(0, 1fr) auto;", css)
        self.assertIn(".pursuit-lane-summary-controls {\n    display: inline-flex;\n    align-items: flex-start;", css)
        self.assertIn(".pursuit-lane-chevron {\n    position: absolute;\n    bottom: 5px;\n    left: 50%;", css)
        self.assertIn("transform: translateX(-50%) rotate(180deg);", css)

        self.assertIn("<dt class=\"pursuit-lane-readonly-label-row\">", template)
        self.assertIn("<span>Lane Name</span>", template)
        self.assertIn("<dt>Match Terms</dt>", template)
        self.assertNotIn("<h3>{{ lane.name }}</h3>", template)
        self.assertNotIn("pursuit-lane-criteria-heading", template)
        self.assertNotIn("pursuit-lane-form-subheading", template)

        self.assertIn("<span class=\"pursuit-lane-match-count\" data-lane-match-count {% if not lane.is_active %}hidden{% endif %}>", template)
        self.assertIn("<span class=\"badge pursuit-lane-status-pill--inactive\" data-lane-status-pill>Inactive</span>", template)
        self.assertIn("pursuit-lane-options--delete-only", template)
        self.assertIn("form=\"delete-lane-{{ lane.id }}\"", template)
        self.assertIn("laneObject.open = true;", template)

    def test_feed_settings_lane_toggle_uses_shared_switch_without_summary_overrides(self):
        template = Path("src/bidlens/templates/pursuit_lanes.html").read_text()
        css = Path("src/bidlens/static/css/styles.css").read_text()

        self.assertIn("admin-setting-toggle admin-setting-toggle--switch", template)
        self.assertIn("class=\"bidlens-switch-field\" aria-label=\"Lane active\"", template)
        self.assertIn("<span class=\"bidlens-switch\" aria-hidden=\"true\"><span class=\"bidlens-switch-knob\"></span></span>", template)
        self.assertIn(".bidlens-switch {\n    position: relative;\n    display: inline-flex;", css)
        self.assertIn("width: 42px;\n    height: 24px;\n    padding: 3px;", css)
        self.assertIn(".bidlens-switch-knob {\n    display: block;\n    width: 18px;\n    height: 18px;", css)
        self.assertIn("input:checked + .bidlens-switch .bidlens-switch-knob", css)
        self.assertIn("transform: translateX(18px);", css)
        self.assertIn(":not(.bidlens-switch):not(.bidlens-switch-knob)", css)

    def test_feed_settings_add_lane_footer_and_compact_readonly_panel(self):
        template = Path("src/bidlens/templates/pursuit_lanes.html").read_text()
        css = Path("src/bidlens/static/css/styles.css").read_text()

        self.assertIn("pursuit-lane-options--create-footer", template)
        self.assertIn("<button type=\"button\" class=\"btn btn-outline-secondary\" onclick=\"cancelLaneEdit(this)\" data-lane-cancel-create>Cancel</button>", template)
        self.assertIn("<button type=\"submit\" class=\"btn btn-primary\" {% if not can_manage_lanes %}disabled{% endif %}>Create Lane</button>", template)
        self.assertIn(".pursuit-lane-options--create-footer {\n    align-items: center;\n    padding-top: 16px;", css)
        self.assertIn("border-top: 1px solid var(--gray-100);", css)
        self.assertIn(".pursuit-lane-options--create-footer .pursuit-lane-editor-actions {\n    width: 100%;\n    justify-content: space-between;", css)
        self.assertIn(".pursuit-lane-detail {\n    display: grid;\n    gap: 10px;\n    padding: 12px 18px 14px;", css)
        self.assertIn(".pursuit-lane-readonly-label-row {\n    display: flex;", css)
        self.assertIn("justify-content: space-between;", css)
        self.assertIn(".pursuit-lane-inline-error", css)

    def test_triage_save_preserves_hidden_digest_values(self):
        profile = OrgProfile(
            org_id=self.org.id,
            digest_recipients="legacy@example.com",
            digest_max_items=42,
            digest_time_local="06:30",
        )
        self.db.add(profile)
        self.db.commit()
        with patch.object(settings, "require_user", return_value=self.admin):
            response = asyncio.run(settings.settings_save(
                self._request(),
                include_keywords="research",
                exclude_keywords="construction",
                include_agencies="HHS",
                exclude_agencies="DoD",
                min_days_out="3",
                max_days_out="60",
                digest_max_items=None,
                digest_recipients=None,
                digest_time_local=None,
                triage_enabled="1",
                db=self.db,
            ))
        self.db.refresh(profile)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(profile.triage_enabled)
        self.assertEqual(profile.digest_recipients, "legacy@example.com")
        self.assertEqual(profile.digest_max_items, 42)
        self.assertEqual(profile.digest_time_local, "06:30")

    def test_triage_toggle_returns_json_for_in_place_updates(self):
        profile = OrgProfile(org_id=self.org.id, triage_enabled=False)
        self.db.add(profile)
        self.db.commit()

        with patch.object(settings, "require_user", return_value=self.admin):
            response = asyncio.run(settings.settings_save(
                self._request(headers={"x-requested-with": "fetch"}),
                include_keywords="",
                exclude_keywords="",
                include_agencies="",
                exclude_agencies="",
                min_days_out="",
                max_days_out="",
                digest_max_items=None,
                digest_recipients=None,
                digest_time_local=None,
                triage_enabled="1",
                db=self.db,
            ))

        payload = json.loads(response.body)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["triage_enabled"])

    def test_legacy_pursuit_lanes_route_redirects_to_feed_settings(self):
        with patch.object(pursuit_lanes, "require_user", return_value=self.admin):
            response = asyncio.run(pursuit_lanes.pursuit_lanes_page(
                self._request("/pursuit-lanes"), self.db
            ))
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/settings?org_id={self.org.id}")

    def test_lane_creation_remains_workspace_scoped_and_returns_to_feed_settings(self):
        with patch.object(pursuit_lanes, "require_user", return_value=self.admin):
            response = asyncio.run(pursuit_lanes.create_pursuit_lane(
                self._request("/pursuit-lanes"),
                name="Federal Health",
                match_terms="HHS, 541611, health, Small Business",
                is_active="1",
                db=self.db,
            ))
        lane = self.db.query(PursuitLane).one()
        self.assertEqual(lane.organization_id, self.org.id)
        self.assertEqual(lane.keywords, ["HHS", "541611", "health", "Small Business"])
        self.assertEqual(lane.agencies, [])
        self.assertEqual(lane.naics, [])
        self.assertEqual(lane.set_asides, [])
        self.assertIsNone(lane.description)
        self.assertEqual(response.headers["location"], f"/settings?org_id={self.org.id}&saved=1")

    def test_lane_creation_returns_json_for_in_place_updates(self):
        with patch.object(pursuit_lanes, "require_user", return_value=self.admin):
            response = asyncio.run(pursuit_lanes.create_pursuit_lane(
                self._request("/pursuit-lanes", headers={"x-requested-with": "fetch"}),
                name="Federal Health",
                match_terms="HHS, health",
                is_active="1",
                db=self.db,
            ))

        payload = json.loads(response.body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["lane"]["name"], "Federal Health")
        self.assertTrue(payload["lane"]["is_active"])
        self.assertEqual(payload["lane"]["match_terms"], ["HHS", "health"])

    def test_lane_edit_remains_workspace_scoped_and_returns_to_feed_settings(self):
        lane = PursuitLane(
            organization_id=self.org.id,
            name="Original",
            agencies=[],
            naics=[],
            keywords=[],
            set_asides=[],
        )
        other_lane = PursuitLane(
            organization_id=self.other_org.id,
            name="Other",
            agencies=[],
            naics=[],
            keywords=[],
            set_asides=[],
        )
        self.db.add_all([lane, other_lane])
        self.db.commit()

        with patch.object(pursuit_lanes, "require_user", return_value=self.admin):
            response = asyncio.run(pursuit_lanes.update_pursuit_lane(
                self._request(f"/pursuit-lanes/{lane.id}"),
                lane_id=lane.id,
                name="Updated",
                match_terms="HHS, 541611, health, Small Business",
                is_active="1",
                db=self.db,
            ))
        self.db.refresh(lane)
        self.db.refresh(other_lane)

        self.assertEqual(lane.name, "Updated")
        self.assertTrue(lane.is_active)
        self.assertEqual(lane.keywords, ["HHS", "541611", "health", "Small Business"])
        self.assertEqual(lane.agencies, [])
        self.assertEqual(lane.naics, [])
        self.assertEqual(lane.set_asides, [])
        self.assertIsNone(lane.description)
        self.assertEqual(lane.organization_id, self.org.id)
        self.assertEqual(other_lane.name, "Other")
        self.assertEqual(response.headers["location"], f"/settings?org_id={self.org.id}&saved=1")

    def test_lane_edit_returns_json_for_in_place_updates(self):
        lane = PursuitLane(
            organization_id=self.org.id,
            name="Original",
            agencies=[],
            naics=[],
            keywords=[],
            set_asides=[],
        )
        self.db.add(lane)
        self.db.commit()

        with patch.object(pursuit_lanes, "require_user", return_value=self.admin):
            response = asyncio.run(pursuit_lanes.update_pursuit_lane(
                self._request(f"/pursuit-lanes/{lane.id}", headers={"x-requested-with": "fetch"}),
                lane_id=lane.id,
                name="Updated",
                match_terms="CMS",
                is_active=None,
                db=self.db,
            ))

        payload = json.loads(response.body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["lane"]["name"], "Updated")
        self.assertFalse(payload["lane"]["is_active"])
        self.assertEqual(payload["lane"]["match_terms_text"], "CMS")

    def test_lane_editor_active_toggle_can_disable_lane(self):
        lane = PursuitLane(
            organization_id=self.org.id,
            name="Active Lane",
            is_active=True,
            agencies=[],
            naics=[],
            keywords=[],
            set_asides=[],
        )
        self.db.add(lane)
        self.db.commit()

        with patch.object(pursuit_lanes, "require_user", return_value=self.admin):
            response = asyncio.run(pursuit_lanes.update_pursuit_lane(
                self._request(f"/pursuit-lanes/{lane.id}"),
                lane_id=lane.id,
                name="Active Lane",
                match_terms="health",
                is_active=None,
                db=self.db,
            ))
        self.db.refresh(lane)

        self.assertFalse(lane.is_active)
        self.assertEqual(response.headers["location"], f"/settings?org_id={self.org.id}&saved=1")

    def test_delete_lane_removes_workspace_lane_with_redirect(self):
        lane = PursuitLane(
            organization_id=self.org.id,
            name="Delete Me",
            agencies=[],
            naics=[],
            keywords=["delete"],
            set_asides=[],
        )
        other_lane = PursuitLane(
            organization_id=self.other_org.id,
            name="Keep Me",
            agencies=[],
            naics=[],
            keywords=[],
            set_asides=[],
        )
        self.db.add_all([lane, other_lane])
        self.db.commit()

        with patch.object(pursuit_lanes, "require_user", return_value=self.admin):
            response = asyncio.run(pursuit_lanes.delete_pursuit_lane(
                self._request(f"/pursuit-lanes/{lane.id}/delete"),
                lane_id=lane.id,
                db=self.db,
            ))

        remaining = self.db.query(PursuitLane).order_by(PursuitLane.id).all()
        self.assertEqual([lane.name for lane in remaining], ["Keep Me"])
        self.assertEqual(response.headers["location"], f"/settings?org_id={self.org.id}&saved=1")

    def test_delete_lane_returns_json_for_in_place_updates(self):
        lane = PursuitLane(
            organization_id=self.org.id,
            name="Delete Me",
            agencies=[],
            naics=[],
            keywords=[],
            set_asides=[],
        )
        self.db.add(lane)
        self.db.commit()

        with patch.object(pursuit_lanes, "require_user", return_value=self.admin):
            response = asyncio.run(pursuit_lanes.delete_pursuit_lane(
                self._request(f"/pursuit-lanes/{lane.id}/delete", headers={"x-requested-with": "fetch"}),
                lane_id=lane.id,
                db=self.db,
            ))

        payload = json.loads(response.body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["lane_id"], lane.id)
        self.assertEqual(self.db.query(PursuitLane).count(), 0)

    def test_legacy_lane_criteria_are_read_as_match_terms(self):
        lane = PursuitLane(
            organization_id=self.org.id,
            name="Legacy",
            agencies=["HHS"],
            naics=["541611"],
            keywords=["Medicaid"],
            set_asides=["Small Business"],
        )

        self.assertEqual(lane_match_terms(lane), ["Medicaid", "HHS", "541611", "Small Business"])

    def test_match_terms_drive_lane_matching_across_opportunity_text(self):
        lane = PursuitLane(
            organization_id=self.org.id,
            name="Health",
            agencies=[],
            naics=[],
            keywords=["CMS", "541611", "Small Business"],
            set_asides=[],
        )
        opportunity = Opportunity(
            organization_id=self.org.id,
            source="manual_import",
            source_record_id="manual-1",
            title="Claims modernization",
            agency="Centers for Medicare and Medicaid Services",
            opportunity_type="RFP",
            posted_date=date(2026, 7, 1),
            response_deadline=date(2026, 8, 1),
            naics="541611",
            set_aside="Small Business",
        )

        reasons = match_lane_to_opportunity(lane, opportunity)

        self.assertIn("Match term matched 541611", reasons)
        self.assertIn("Match term matched Small Business", reasons)


if __name__ == "__main__":
    unittest.main()
