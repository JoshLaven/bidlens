import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import (
    Opportunity, Organization, OrganizationMembership, SalesforceConnection,
    SalesforceOAuthState, User,
)
from bidlens.routes import api, integrations
from bidlens.services.integration_credentials import encrypt_credentials
from bidlens.services.salesforce import SalesforceService


class SalesforceConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org_a = Organization(name="Workspace A", slug="workspace-a")
        self.org_b = Organization(name="Workspace B", slug="workspace-b")
        self.db.add_all([self.org_a, self.org_b])
        self.db.flush()
        self.admin = User(email="admin@example.test", organization_id=self.org_a.id)
        self.member = User(email="member@example.test", organization_id=self.org_a.id)
        self.db.add_all([self.admin, self.member])
        self.db.flush()
        self.db.add_all([
            OrganizationMembership(organization_id=self.org_a.id, user_id=self.admin.id, role="admin"),
            OrganizationMembership(organization_id=self.org_a.id, user_id=self.member.id, role="member"),
        ])
        self.db.commit()
        for user, role in ((self.admin, "admin"), (self.member, "member")):
            setattr(user, "current_organization_id", self.org_a.id)
            setattr(user, "current_role", role)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _connection(self, workspace, status="connected", token="access-secret"):
        connection = SalesforceConnection(
            workspace_id=workspace.id,
            instance_url=f"https://{workspace.slug}.my.salesforce.com",
            salesforce_org_id=f"org-{workspace.id}",
            connected_user_id=f"user-{workspace.id}",
            connected_username=f"admin@{workspace.slug}.test",
            encrypted_access_token=encrypt_credentials({"token": token}),
            encrypted_refresh_token=encrypt_credentials({"token": f"refresh-{token}"}),
            status=status,
            connected_at=datetime.utcnow(),
            last_connection_success_at=datetime.utcnow(),
        )
        self.db.add(connection)
        self.db.commit()
        return connection

    def _render(self, status, connection=None):
        template = integrations.templates.env.get_template("salesforce_configuration.html")
        return template.render(
            request=SimpleNamespace(query_params={"org_id": str(self.org_a.id)}),
            user=self.admin,
            connection=connection,
            connection_status=status,
            last_sync=None,
            connected_success=False,
            tested=None,
            disconnected=False,
            validation_result=None,
            validation_timestamp=None,
        )

    def _salesforce_fields(self, *, missing=(), stage_values=None, intake_status_values=None, intake_source_values=None, extra_required=None):
        missing = set(missing)
        stage_values = ["Prospecting"] if stage_values is None else stage_values
        intake_status_values = ["Prospect_Feed"] if intake_status_values is None else intake_status_values
        intake_source_values = ["SAM", "Grants.gov", "GovWin"] if intake_source_values is None else intake_source_values

        def field(name, label, *, createable=True, updateable=True, nillable=True, values=None):
            return {
                "name": name,
                "label": label,
                "createable": createable,
                "updateable": updateable,
                "nillable": nillable,
                "defaultedOnCreate": False,
                "picklistValues": [
                    {"active": True, "value": value}
                    for value in (values or [])
                ],
            }

        fields = [
            field("Name", "Opportunity Name", nillable=False),
            field("StageName", "Stage", updateable=False, nillable=False, values=stage_values),
            field("CloseDate", "Close Date", nillable=False),
            field("Description", "Description"),
            field("External_Source_ID__c", "External Source ID", updateable=False),
            field("Intake_Status__c", "Intake Status", values=intake_status_values),
            field("Intake_Source__c", "Intake Source", updateable=False, values=intake_source_values),
        ]
        fields = [item for item in fields if item["name"] not in missing]
        fields.extend(extra_required or [])
        return {
            "queryable": True,
            "createable": True,
            "updateable": True,
            "fields": fields,
        }

    def _readiness_service(self, *, status="connected", token="access-secret"):
        self._connection(self.org_a, status=status, token=token)
        service = SalesforceService(db=self.db, workspace_id=self.org_a.id)
        service.test_connection = Mock(return_value={"ok": True})
        service.describe_opportunity = Mock(return_value=self._salesforce_fields())
        service.create_opportunity = Mock(side_effect=AssertionError("validation must not create Salesforce records"))
        service.update_opportunity = Mock(side_effect=AssertionError("validation must not update Salesforce records"))
        return service

    def test_connections_and_tokens_are_workspace_scoped(self):
        a = self._connection(self.org_a, token="workspace-a-secret")
        b = self._connection(self.org_b, token="workspace-b-secret")
        service_a = SalesforceService(db=self.db, workspace_id=self.org_a.id)
        service_b = SalesforceService(db=self.db, workspace_id=self.org_b.id)
        self.assertEqual(service_a.connection.id, a.id)
        self.assertEqual(service_b.connection.id, b.id)
        self.assertEqual(service_a._token("access"), "workspace-a-secret")
        self.assertEqual(service_b._token("access"), "workspace-b-secret")
        self.assertNotEqual(service_a._token("access"), service_b._token("access"))

    def test_statuses_render_only_appropriate_actions_and_no_secrets(self):
        cases = {
            "not_connected": ("Not Connected", ["Connect Salesforce"], ["Test Connection", "Disconnect"]),
            "connected": ("Connected", ["Test Connection", "Reconnect / Reauthorize", "Disconnect", "Open Salesforce"], ["Connect Salesforce"]),
            "reauthorization_required": ("Requires Reauthorization", ["Reauthorize", "Disconnect"], ["Test Connection", "Open Salesforce"]),
            "connection_error": ("Connection Error", ["Test Again", "Reauthorize", "Disconnect"], ["Open Salesforce"]),
        }
        for status, (label, present, absent) in cases.items():
            connection = None if status == "not_connected" else self._connection(self.org_a, status=status, token="never-render-me")
            html = self._render(status, connection)
            self.assertIn(label, html)
            for value in present:
                self.assertIn(value, html)
            for value in absent:
                self.assertNotIn(f">{value}<", html)
            self.assertNotIn("never-render-me", html)
            self.assertNotIn("refresh-never-render-me", html)
            if connection:
                self.db.delete(connection)
                self.db.commit()

    def test_revoked_refresh_marks_reauthorization_required_without_raw_error(self):
        connection = self._connection(self.org_a)
        connection.encrypted_access_token = None
        response = Mock(ok=False, status_code=400, text="refresh-secret should not persist")
        with patch("bidlens.services.salesforce.requests.post", return_value=response):
            self.assertFalse(SalesforceService(db=self.db, workspace_id=self.org_a.id).is_authorized())
        self.assertEqual(connection.status, "reauthorization_required")
        self.assertNotIn("refresh-secret", connection.last_error)

    def test_connection_updates_success_timestamp(self):
        connection = self._connection(self.org_a)
        connection.last_connection_success_at = None
        response = Mock(ok=True, status_code=200)
        with patch("bidlens.services.salesforce.requests.get", return_value=response):
            SalesforceService(db=self.db, workspace_id=self.org_a.id).test_connection()
        self.assertEqual(connection.status, "connected")
        self.assertIsNotNone(connection.last_connection_success_at)

    def test_readiness_validation_ready_for_fully_configured_salesforce(self):
        service = self._readiness_service()

        result = service.validate_readiness()

        self.assertEqual(result["overall_status"], "ready")
        messages = [check["message"] for check in result["checks"]]
        self.assertIn("Opportunity object is accessible.", messages)
        self.assertIn("Required field External Source ID is available.", messages)
        service.test_connection.assert_called_once()
        service.describe_opportunity.assert_called_once()
        service.create_opportunity.assert_not_called()
        service.update_opportunity.assert_not_called()

    def test_readiness_validation_missing_required_field_requires_action(self):
        service = self._readiness_service()
        service.describe_opportunity.return_value = self._salesforce_fields(
            missing={"External_Source_ID__c"}
        )

        result = service.validate_readiness()

        self.assertEqual(result["overall_status"], "action_required")
        self.assertTrue(any(
            check["status"] == "failed"
            and "External Source ID" in check["message"]
            for check in result["checks"]
        ))

    def test_readiness_validation_missing_supported_intake_source_requires_action(self):
        service = self._readiness_service()
        service.describe_opportunity.return_value = self._salesforce_fields(
            intake_source_values=["SAM", "Grants.gov"]
        )

        result = service.validate_readiness()

        self.assertEqual(result["overall_status"], "action_required")
        missing_check = next(
            check for check in result["checks"]
            if check["key"] == "intake_source_values"
        )
        self.assertEqual(missing_check["status"], "failed")
        self.assertEqual(missing_check["message"], "Missing Intake Source values.")
        self.assertEqual(missing_check["detail"], "GovWin")
        self.assertFalse(any(
            check["status"] == "failed"
            and "BidLens" in f"{check['message']} {check.get('detail') or ''}"
            for check in result["checks"]
        ))

    def test_readiness_validation_missing_prospecting_requires_action(self):
        service = self._readiness_service()
        service.describe_opportunity.return_value = self._salesforce_fields(
            stage_values=["Qualification"]
        )

        result = service.validate_readiness()

        self.assertEqual(result["overall_status"], "action_required")
        self.assertTrue(any(
            check["status"] == "failed"
            and "Missing Stage value: Prospecting" in check["message"]
            for check in result["checks"]
        ))

    def test_readiness_validation_missing_prospect_feed_requires_action(self):
        service = self._readiness_service()
        service.describe_opportunity.return_value = self._salesforce_fields(
            intake_status_values=["New"]
        )

        result = service.validate_readiness()

        self.assertEqual(result["overall_status"], "action_required")
        self.assertTrue(any(
            check["status"] == "failed"
            and "Missing Intake Status value: Prospect_Feed" in check["message"]
            for check in result["checks"]
        ))

    def test_readiness_validation_surfaces_additional_required_fields(self):
        service = self._readiness_service()
        service.describe_opportunity.return_value = self._salesforce_fields(
            extra_required=[{
                "name": "Customer_Required__c",
                "label": "Customer Required",
                "createable": True,
                "updateable": True,
                "nillable": False,
                "defaultedOnCreate": False,
                "picklistValues": [],
            }]
        )

        result = service.validate_readiness()

        self.assertEqual(result["overall_status"], "action_required")
        self.assertTrue(any(
            check["status"] == "failed"
            and "additional Opportunity fields" in check["message"]
            and "Customer_Required__c" in (check.get("detail") or "")
            for check in result["checks"]
        ))

    def test_readiness_validation_handles_reauthorization_required(self):
        service = self._readiness_service(status="reauthorization_required")

        result = service.validate_readiness()

        self.assertEqual(result["overall_status"], "action_required")
        self.assertTrue(any(
            "requires reauthorization" in check["message"]
            for check in result["checks"]
        ))
        service.test_connection.assert_not_called()
        service.describe_opportunity.assert_not_called()

    def test_admin_can_run_workspace_scoped_validation(self):
        validation = {
            "overall_status": "ready",
            "overall_label": "Ready",
            "checks": [{
                "key": "connection",
                "label": "Salesforce connection",
                "status": "passed",
                "message": "Salesforce is connected for this workspace.",
                "detail": None,
            }],
        }
        service = Mock()
        service.validate_readiness.return_value = validation
        with patch.object(integrations, "SalesforceService", return_value=service) as service_class, patch.object(
            integrations, "_admin_or_redirect", return_value=(self.admin, None)
        ):
            response = asyncio.run(integrations.validate_salesforce_setup(
                SimpleNamespace(query_params={"org_id": str(self.org_a.id)}),
                self.db,
            ))

        self.assertEqual(response.status_code, 200)
        service_class.assert_called_once_with(db=self.db, workspace_id=self.org_a.id)
        service.validate_readiness.assert_called_once()
        body = response.body.decode()
        self.assertIn("Salesforce Readiness", body)
        self.assertIn("Ready", body)

    def test_integrations_page_exposes_only_configure_for_salesforce(self):
        html = integrations.templates.env.get_template("integrations.html").render(
            request=SimpleNamespace(query_params={"org_id": str(self.org_a.id)}),
            user=self.admin,
            center=SimpleNamespace(
                salesforce=SimpleNamespace(
                    health=SimpleNamespace(
                        level="healthy",
                        label="Ready",
                        summary="Salesforce is connected.",
                        checks=[],
                    ),
                    directory_status=SimpleNamespace(level="healthy", label="Ready"),
                    connected=True,
                    connection_health="Connected",
                    instance_url="https://workspace-a.my.salesforce.com",
                    mappings=[],
                    validation_error=None,
                )
            ),
        )

        self.assertIn("integration-directory-list", html)
        self.assertIn("integration-directory-card", html)
        self.assertNotIn("integration-directory-grid", html)
        self.assertIn("Configure", html)
        self.assertIn("Configure Salesforce", html)
        self.assertIn("CRM destination for eligible BidLens opportunities.", html)
        self.assertIn("configuration-health--healthy", html)
        self.assertIn("Ready", html)
        self.assertIn("Microsoft 365", html)
        self.assertIn("Outlook", html)
        self.assertIn("Teams", html)
        self.assertIn("HubSpot", html)
        self.assertIn("SharePoint", html)
        self.assertIn("Coming soon", html)
        self.assertNotIn("Connection Status", html)
        self.assertNotIn("Current Configuration", html)
        self.assertNotIn("Connector Health", html)
        self.assertNotIn("Required Opportunity fields verified", html)
        self.assertNotIn("External_Source_ID__c", html)
        self.assertNotIn("Validate Setup", html)
        self.assertNotIn("Inspect Opportunity Requirements", html)
        self.assertNotIn("opportunity-create-requirements", html)

    def test_salesforce_directory_status_uses_existing_connection_state(self):
        cases = [
            (
                {
                    "connected": False,
                    "connection_status": "not_connected",
                    "inspection": None,
                    "error": None,
                    "instance_url": None,
                },
                {"level": "neutral", "label": "Not connected"},
            ),
            (
                {
                    "connected": False,
                    "connection_status": "reauthorization_required",
                    "inspection": None,
                    "error": None,
                    "instance_url": "https://workspace-a.my.salesforce.com",
                },
                {"level": "warning", "label": "Requires reauthorization"},
            ),
            (
                {
                    "connected": False,
                    "connection_status": "connection_error",
                    "inspection": None,
                    "error": "Salesforce could not validate the connection.",
                    "instance_url": "https://workspace-a.my.salesforce.com",
                },
                {"level": "warning", "label": "Connection error"},
            ),
            (
                {
                    "connected": True,
                    "connection_status": "connected",
                    "inspection": {
                        "required_fields_verified": True,
                        "default_stage_valid": True,
                        "intake_source_values": ["SAM", "Grants.gov", "GovWin"],
                        "field_mappings_valid": True,
                    },
                    "error": None,
                    "instance_url": "https://workspace-a.my.salesforce.com",
                },
                {"level": "healthy", "label": "Ready"},
            ),
            (
                {
                    "connected": True,
                    "connection_status": "connected",
                    "inspection": {
                        "required_fields_verified": True,
                        "default_stage_valid": True,
                        "intake_source_values": ["SAM", "Grants.gov"],
                        "field_mappings_valid": True,
                    },
                    "error": None,
                    "instance_url": "https://workspace-a.my.salesforce.com",
                },
                {"level": "required", "label": "Configuration required"},
            ),
        ]
        for snapshot, expected_status in cases:
            center = integrations._configuration_center_context(
                self.db,
                organization_id=self.org_a.id,
                profile=None,
                salesforce_snapshot=snapshot,
                now=datetime(2026, 7, 20, 12, 0),
            )
            self.assertEqual(center["salesforce"]["directory_status"], expected_status)

    def test_readiness_actions_are_consolidated_on_configuration_page(self):
        connection = self._connection(self.org_a)
        html = self._render("connected", connection)

        self.assertNotIn("id=\"salesforce-actions-heading\"", html)
        ordered_actions = [
            "Validate Setup",
            "Test Connection",
            "Reconnect / Reauthorize",
            "Open Salesforce",
            "Disconnect",
        ]
        positions = [html.index(action) for action in ordered_actions]
        self.assertEqual(positions, sorted(positions))

    def test_ready_validation_renders_customer_friendly_summary(self):
        connection = self._connection(self.org_a)
        html = integrations.templates.env.get_template("salesforce_configuration.html").render(
            request=SimpleNamespace(query_params={"org_id": str(self.org_a.id)}),
            user=self.admin,
            connection=connection,
            connection_status="connected",
            last_sync=None,
            connected_success=False,
            tested=None,
            disconnected=False,
            validation_timestamp=datetime.utcnow(),
            validation_result={
                "overall_status": "ready",
                "overall_label": "Ready",
                "checks": [
                    {"key": "connection", "label": "Salesforce connection", "status": "passed", "message": "Salesforce is connected for this workspace.", "detail": None},
                    {"key": "field_External_Source_ID__c", "label": "External Source ID", "status": "passed", "message": "Required field External Source ID is available.", "detail": None},
                ],
            },
        )

        self.assertIn("Connected to Salesforce", html)
        self.assertIn("Opportunity object accessible", html)
        self.assertIn("Required BidLens fields found", html)
        self.assertIn("Required picklist values found", html)
        self.assertIn("Ready to receive BidLens opportunities", html)
        self.assertNotIn("Required field External Source ID is available.", html)

    def test_action_required_validation_renders_only_items_needing_attention(self):
        connection = self._connection(self.org_a)
        html = integrations.templates.env.get_template("salesforce_configuration.html").render(
            request=SimpleNamespace(query_params={"org_id": str(self.org_a.id)}),
            user=self.admin,
            connection=connection,
            connection_status="connected",
            last_sync=None,
            connected_success=False,
            tested=None,
            disconnected=False,
            validation_timestamp=datetime.utcnow(),
            validation_result={
                "overall_status": "action_required",
                "overall_label": "Action required",
                "checks": [
                    {"key": "connection", "label": "Salesforce connection", "status": "passed", "message": "Salesforce is connected for this workspace.", "detail": None},
                    {"key": "field_External_Source_ID__c", "label": "External Source ID", "status": "failed", "message": "Missing required field: External Source ID.", "detail": None},
                ],
            },
        )

        self.assertIn("Missing required field: External Source ID.", html)
        self.assertNotIn("Salesforce is connected for this workspace.", html)
        self.assertNotIn("Passed ·", html)

    def test_ui_no_longer_references_inspect_opportunity_requirements(self):
        for relative_path in [
            "src/bidlens/templates/integrations.html",
            "src/bidlens/templates/integrations 2.html",
            "src/bidlens/templates/salesforce_admin.html",
            "src/bidlens/templates/salesforce_configuration.html",
        ]:
            contents = Path(relative_path).read_text()
            self.assertNotIn("Inspect Opportunity Requirements", contents)
            self.assertNotIn("opportunity-create-requirements", contents)

    def test_member_cannot_run_validation(self):
        with patch.object(integrations, "get_current_user", return_value=self.member), patch.object(
            integrations, "attach_request_user_context", return_value=self.member
        ):
            response = asyncio.run(integrations.validate_salesforce_setup(
                SimpleNamespace(query_params={"org_id": str(self.org_a.id)}),
                self.db,
            ))

        self.assertEqual(response.status_code, 303)

    def test_validation_rendering_does_not_expose_stored_secrets(self):
        connection = self._connection(self.org_a, token="never-render-me")
        html = integrations.templates.env.get_template("salesforce_configuration.html").render(
            request=SimpleNamespace(query_params={"org_id": str(self.org_a.id)}),
            user=self.admin,
            connection=connection,
            connection_status="connected",
            last_sync=None,
            connected_success=False,
            tested=None,
            disconnected=False,
            validation_timestamp=datetime.utcnow(),
            validation_result={
                "overall_status": "ready",
                "overall_label": "Ready",
                "checks": [{
                    "key": "connection",
                    "label": "Salesforce connection",
                    "status": "passed",
                    "message": "Salesforce is connected for this workspace.",
                    "detail": None,
                }],
            },
        )
        self.assertNotIn("never-render-me", html)
        self.assertNotIn("refresh-never-render-me", html)

    def test_only_workspace_admin_can_open_configuration(self):
        with patch.object(integrations, "get_current_user", return_value=self.admin), patch.object(
            integrations, "attach_request_user_context", return_value=self.admin
        ):
            user, redirect = integrations._admin_or_redirect(SimpleNamespace(), self.db)
            self.assertEqual(user.id, self.admin.id)
            self.assertIsNone(redirect)
        with patch.object(integrations, "get_current_user", return_value=self.member), patch.object(
            integrations, "attach_request_user_context", return_value=self.member
        ):
            user, redirect = integrations._admin_or_redirect(SimpleNamespace(), self.db)
            self.assertIsNone(user)
            self.assertEqual(redirect.status_code, 303)

    def test_disconnect_clears_tokens_and_preserves_salesforce_opportunity_reference(self):
        connection = self._connection(self.org_a)
        opportunity = Opportunity(
            organization_id=self.org_a.id, source="sam.gov", source_record_id="notice-1",
            title="Existing link", agency="Agency", opportunity_type="Solicitation",
            posted_date=datetime.utcnow().date(), response_deadline=(datetime.utcnow() + timedelta(days=10)).date(),
            salesforce_opportunity_id="006-preserved", salesforce_opportunity_url="https://example.test/006-preserved",
        )
        self.db.add(opportunity)
        self.db.commit()
        with patch.object(integrations, "_admin_or_redirect", return_value=(self.admin, None)):
            response = asyncio.run(integrations.disconnect_salesforce(SimpleNamespace(), self.db))
        self.assertEqual(response.status_code, 303)
        self.assertIsNone(connection.encrypted_access_token)
        self.assertIsNone(connection.encrypted_refresh_token)
        self.assertEqual(connection.status, "not_connected")
        self.assertEqual(opportunity.salesforce_opportunity_id, "006-preserved")

    def test_oauth_state_rejects_replay_mismatch_and_expiration(self):
        state = "single-use-state"
        record = SalesforceOAuthState(
            state_digest=hashlib.sha256(state.encode()).hexdigest(),
            encrypted_code_verifier=encrypt_credentials({"verifier": "pkce-secret"}),
            workspace_id=self.org_a.id,
            user_id=self.admin.id,
            return_path="/workspace-management/business-systems/salesforce",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        self.db.add(record)
        self.db.commit()
        service = Mock()
        service.exchange_authorization_code.return_value = {}
        with patch.object(api, "get_current_user", return_value=self.admin), patch.object(api, "SalesforceService", return_value=service):
            response = api.salesforce_oauth_callback(SimpleNamespace(url_for=lambda _: "callback"), code="code", state=state, db=self.db)
            self.assertEqual(response.status_code, 303)
            with self.assertRaises(HTTPException):
                api.salesforce_oauth_callback(SimpleNamespace(url_for=lambda _: "callback"), code="code", state=state, db=self.db)

        for user_id, expires in ((self.member.id, datetime.now(timezone.utc) + timedelta(minutes=5)), (self.admin.id, datetime.now(timezone.utc) - timedelta(seconds=1))):
            other_state = f"state-{user_id}-{expires.timestamp()}"
            self.db.add(SalesforceOAuthState(
                state_digest=hashlib.sha256(other_state.encode()).hexdigest(),
                encrypted_code_verifier=encrypt_credentials({"verifier": "v"}),
                workspace_id=self.org_a.id, user_id=user_id,
                return_path="/", expires_at=expires,
            ))
            self.db.commit()
            with patch.object(api, "get_current_user", return_value=self.admin):
                with self.assertRaises(HTTPException):
                    api.salesforce_oauth_callback(SimpleNamespace(), code="code", state=other_state, db=self.db)

    def test_oauth_state_accepts_postgresql_style_naive_expiration(self):
        naive_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        self._assert_oauth_expiration_succeeds(naive_utc + timedelta(minutes=5))

    def test_oauth_state_accepts_timezone_aware_expiration(self):
        self._assert_oauth_expiration_succeeds(
            datetime.now(timezone.utc) + timedelta(minutes=5)
        )

    def _assert_oauth_expiration_succeeds(self, expires_at):
        state = f"timestamp-state-{expires_at.timestamp()}"
        self.db.add(SalesforceOAuthState(
            state_digest=hashlib.sha256(state.encode()).hexdigest(),
            encrypted_code_verifier=encrypt_credentials({"verifier": "pkce-secret"}),
            workspace_id=self.org_a.id,
            user_id=self.admin.id,
            return_path="/workspace-management/business-systems/salesforce",
            expires_at=expires_at,
        ))
        self.db.commit()
        service = Mock()
        service.exchange_authorization_code.return_value = {}
        with patch.object(api, "get_current_user", return_value=self.admin), patch.object(
            api, "SalesforceService", return_value=service
        ):
            response = api.salesforce_oauth_callback(
                SimpleNamespace(url_for=lambda _: "callback"),
                code="code",
                state=state,
                db=self.db,
            )
        self.assertEqual(response.status_code, 303)


if __name__ == "__main__":
    unittest.main()
