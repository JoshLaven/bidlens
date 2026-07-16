import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
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
        )

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
