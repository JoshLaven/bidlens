import base64
import asyncio
import datetime as dt
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from bidlens.database import Base
from bidlens.models import (
    Event,
    ExternalIntegrationConnection,
    ExternalIntegrationOAuthState,
    Organization,
    OrganizationMembership,
    User,
    Workspace,
)
from bidlens.routes import integrations
from bidlens.services.integration_credentials import decrypt_credentials, encrypt_credentials
from bidlens.services.microsoft import (
    MICROSOFT_SCOPES,
    PROVIDER_MICROSOFT,
    STATUS_CONNECTED,
    STATUS_DISCONNECTED,
    STATUS_REAUTHORIZATION_REQUIRED,
    MicrosoftConnectionError,
    MicrosoftConnectionService,
    connection_status_summary,
    generate_pkce_pair,
    safe_return_path,
    state_digest,
)


def _response(ok=True, status_code=200, payload=None):
    response = Mock()
    response.ok = ok
    response.status_code = status_code
    response.json.return_value = payload or {}
    return response


def _id_token(*, oid="ms-user-1", tid="tenant-1"):
    payload = {"oid": oid, "tid": tid}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


class MicrosoftConnectionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.org = Organization(name="Microsoft Org", slug="microsoft-org")
        self.db.add(self.org)
        self.db.flush()
        self.workspace = Workspace(organization_id=self.org.id, name="Microsoft Workspace", slug="microsoft-workspace")
        self.user = User(email="casey@example.com", name="Casey", organization_id=self.org.id)
        self.other_user = User(email="other@example.com", name="Other", organization_id=self.org.id)
        self.db.add_all([self.workspace, self.user, self.other_user])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _service(self, user=None):
        return MicrosoftConnectionService(db=self.db, workspace=self.workspace, user=user or self.user)

    @patch("bidlens.services.microsoft.config.MICROSOFT_CLIENT_ID", "client")
    @patch("bidlens.services.microsoft.config.MICROSOFT_CLIENT_SECRET", "secret")
    @patch("bidlens.services.microsoft.config.MICROSOFT_REDIRECT_URI", "https://app.example.com/integrations/microsoft/oauth/callback")
    @patch("bidlens.services.microsoft.requests.get")
    def test_complete_connection_encrypts_tokens_and_status_is_safe(self, mock_get):
        mock_get.return_value = _response(payload={
            "id": "ms-user-1",
            "displayName": "Microsoft Casey",
            "userPrincipalName": "casey@microsoft.example",
        })

        connection = self._service().complete_connection({
            "access_token": "plain-access-token",
            "refresh_token": "plain-refresh-token",
            "expires_in": 3600,
            "scope": " ".join(MICROSOFT_SCOPES),
            "id_token": _id_token(),
        })
        self.db.commit()

        self.assertNotIn("plain-access-token", connection.encrypted_access_token)
        self.assertNotIn("plain-refresh-token", connection.encrypted_refresh_token)
        self.assertEqual(decrypt_credentials(connection.encrypted_access_token)["token"], "plain-access-token")
        status = connection_status_summary(connection)
        self.assertNotIn("encrypted_access_token", status)
        self.assertEqual(status["connected_email"], "casey@microsoft.example")
        self.assertEqual(connection.external_tenant_id, "tenant-1")
        self.assertEqual(self.db.query(Event).filter(Event.event_type == "integration_lifecycle").count(), 1)

    @patch("bidlens.services.microsoft.requests.get")
    def test_identity_mismatch_between_id_token_and_graph_is_rejected(self, mock_get):
        mock_get.return_value = _response(payload={"id": "graph-user", "userPrincipalName": "casey@example.com"})

        with self.assertRaises(MicrosoftConnectionError) as context:
            self._service().complete_connection({
                "access_token": "access",
                "refresh_token": "refresh",
                "id_token": _id_token(oid="different-user"),
            })

        self.assertEqual(context.exception.code, "identity_mismatch")

    @patch("bidlens.services.microsoft.config.MICROSOFT_CLIENT_ID", "client")
    @patch("bidlens.services.microsoft.config.MICROSOFT_CLIENT_SECRET", "secret")
    @patch("bidlens.services.microsoft.config.MICROSOFT_REDIRECT_URI", "https://app.example.com/integrations/microsoft/oauth/callback")
    @patch("bidlens.services.microsoft.requests.post")
    def test_refresh_rotates_refresh_token_when_supplied(self, mock_post):
        connection = ExternalIntegrationConnection(
            workspace_id=self.workspace.id,
            user_id=self.user.id,
            provider=PROVIDER_MICROSOFT,
            connection_status=STATUS_CONNECTED,
            external_user_id="ms-user-1",
            encrypted_access_token="",
            encrypted_refresh_token="",
            access_token_expires_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1),
        )
        self.db.add(connection)
        self.db.flush()
        connection.encrypted_access_token = encrypt_credentials({"token": "old-access"})
        connection.encrypted_refresh_token = encrypt_credentials({"token": "old-refresh"})
        mock_post.return_value = _response(payload={
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        })

        token = self._service().refresh_access_token(connection)

        self.assertEqual(token, "new-access")
        self.assertEqual(decrypt_credentials(connection.encrypted_refresh_token)["token"], "new-refresh")

    @patch("bidlens.services.microsoft.config.MICROSOFT_CLIENT_ID", "client")
    @patch("bidlens.services.microsoft.config.MICROSOFT_CLIENT_SECRET", "secret")
    @patch("bidlens.services.microsoft.config.MICROSOFT_REDIRECT_URI", "https://app.example.com/integrations/microsoft/oauth/callback")
    @patch("bidlens.services.microsoft.requests.post")
    def test_refresh_omitted_refresh_token_does_not_erase_existing_one(self, mock_post):
        connection = ExternalIntegrationConnection(
            workspace_id=self.workspace.id,
            user_id=self.user.id,
            provider=PROVIDER_MICROSOFT,
            connection_status=STATUS_CONNECTED,
            external_user_id="ms-user-1",
            encrypted_access_token=encrypt_credentials({"token": "old-access"}),
            encrypted_refresh_token=encrypt_credentials({"token": "keep-refresh"}),
        )
        self.db.add(connection)
        self.db.flush()
        mock_post.return_value = _response(payload={"access_token": "new-access", "expires_in": 3600})

        self._service().refresh_access_token(connection)

        self.assertEqual(decrypt_credentials(connection.encrypted_refresh_token)["token"], "keep-refresh")

    @patch("bidlens.services.microsoft.config.MICROSOFT_CLIENT_ID", "client")
    @patch("bidlens.services.microsoft.config.MICROSOFT_CLIENT_SECRET", "secret")
    @patch("bidlens.services.microsoft.config.MICROSOFT_REDIRECT_URI", "https://app.example.com/integrations/microsoft/oauth/callback")
    @patch("bidlens.services.microsoft.requests.post")
    def test_invalid_grant_marks_reauthorization_required_without_destroying_tokens(self, mock_post):
        connection = ExternalIntegrationConnection(
            workspace_id=self.workspace.id,
            user_id=self.user.id,
            provider=PROVIDER_MICROSOFT,
            connection_status=STATUS_CONNECTED,
            external_user_id="ms-user-1",
            encrypted_access_token=encrypt_credentials({"token": "old-access"}),
            encrypted_refresh_token=encrypt_credentials({"token": "keep-refresh"}),
        )
        self.db.add(connection)
        self.db.flush()
        mock_post.return_value = _response(ok=False, status_code=400)

        with self.assertRaises(MicrosoftConnectionError):
            self._service().refresh_access_token(connection)

        self.assertEqual(connection.connection_status, STATUS_REAUTHORIZATION_REQUIRED)
        self.assertEqual(decrypt_credentials(connection.encrypted_refresh_token)["token"], "keep-refresh")

    def test_access_token_rejects_cross_user_connection(self):
        connection = ExternalIntegrationConnection(
            workspace_id=self.workspace.id,
            user_id=self.other_user.id,
            provider=PROVIDER_MICROSOFT,
            connection_status=STATUS_CONNECTED,
        )

        with self.assertRaises(MicrosoftConnectionError):
            self._service().access_token_for_connection(connection)

    def test_disconnect_clears_credentials_only_for_authenticated_user(self):
        connection = ExternalIntegrationConnection(
            workspace_id=self.workspace.id,
            user_id=self.user.id,
            provider=PROVIDER_MICROSOFT,
            connection_status=STATUS_CONNECTED,
            encrypted_access_token=encrypt_credentials({"token": "access"}),
            encrypted_refresh_token=encrypt_credentials({"token": "refresh"}),
        )
        self.db.add(connection)
        self.db.commit()

        self._service().disconnect()

        self.assertEqual(connection.connection_status, STATUS_DISCONNECTED)
        self.assertIsNone(connection.encrypted_access_token)
        self.assertIsNone(connection.encrypted_refresh_token)

    def test_one_connection_per_user_provider_scope(self):
        self.db.add_all([
            ExternalIntegrationConnection(workspace_id=self.workspace.id, user_id=self.user.id, provider=PROVIDER_MICROSOFT),
            ExternalIntegrationConnection(workspace_id=self.workspace.id, user_id=self.user.id, provider=PROVIDER_MICROSOFT),
        ])

        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

    def test_oauth_state_stores_digest_and_encrypted_verifier(self):
        state = "state-secret"
        verifier, _ = generate_pkce_pair()
        record = ExternalIntegrationOAuthState(
            provider=PROVIDER_MICROSOFT,
            state_digest=state_digest(state),
            encrypted_code_verifier=encrypt_credentials({"verifier": verifier}),
            workspace_id=self.workspace.id,
            user_id=self.user.id,
            return_path=safe_return_path("//evil.example.com"),
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10),
        )
        self.db.add(record)
        self.db.commit()

        self.assertNotEqual(record.state_digest, state)
        self.assertNotIn(verifier, record.encrypted_code_verifier)
        self.assertEqual(record.return_path, "/integrations/microsoft")

    def test_workspace_adoption_uses_member_counts_without_roster_rows(self):
        third_user = User(email="third@example.com", name="Third", organization_id=self.org.id)
        self.db.add(third_user)
        self.db.flush()
        self.db.add_all([
            OrganizationMembership(organization_id=self.org.id, user_id=self.user.id, role="admin"),
            OrganizationMembership(organization_id=self.org.id, user_id=self.other_user.id, role="member"),
            OrganizationMembership(organization_id=self.org.id, user_id=third_user.id, role="member"),
            ExternalIntegrationConnection(
                workspace_id=self.workspace.id,
                user_id=self.user.id,
                provider=PROVIDER_MICROSOFT,
                connection_status=STATUS_CONNECTED,
            ),
            ExternalIntegrationConnection(
                workspace_id=self.workspace.id,
                user_id=self.other_user.id,
                provider=PROVIDER_MICROSOFT,
                connection_status=STATUS_DISCONNECTED,
            ),
        ])
        self.db.commit()

        self.assertEqual(
            integrations._microsoft_adoption_summary(self.db, self.workspace),
            {"connected": 1, "not_connected": 2, "total": 3},
        )

    def test_microsoft_configuration_template_uses_workspace_capability_architecture(self):
        html = integrations.templates.env.get_template("microsoft_connection.html").render(
            request=SimpleNamespace(query_params={}),
            workspace=self.workspace,
            status={
                "connected": True,
                "status": STATUS_CONNECTED,
                "level": "success",
                "label": "Connected",
                "connected_at": dt.datetime(2026, 8, 1),
                "last_verified_at": dt.datetime(2026, 8, 2),
            },
            adoption={"connected": 1, "not_connected": 1, "total": 2},
            scope_summary="Mail.Send, Mail.ReadWrite",
            is_admin=True,
            manage_users_url=f"/admin/organizations/{self.org.id}/users?org_id={self.org.id}",
        )

        self.assertIn("<h1>Microsoft 365</h1>", html)
        self.assertIn("Workspace Integration", html)
        self.assertIn("Capabilities", html)
        self.assertIn("Outlook Email", html)
        self.assertIn("Enabled", html)
        for capability in ("Calendar", "Teams", "OneDrive", "SharePoint"):
            self.assertIn(capability, html)
        self.assertEqual(html.count("Coming Soon"), 4)
        self.assertIn("Workspace Adoption", html)
        self.assertIn("1 user", html)
        self.assertIn("Manage Users", html)
        self.assertNotIn("admin-table", html)
        self.assertNotIn("Connected email", html)

    def test_microsoft_configuration_template_defaults_missing_adoption_to_zero(self):
        html = integrations.templates.env.get_template("microsoft_connection.html").render(
            request=SimpleNamespace(query_params={}),
            workspace=self.workspace,
            status={
                "connected": False,
                "status": "not_connected",
                "level": "neutral",
                "label": "Not connected",
            },
            scope_summary="Mail.Send, Mail.ReadWrite",
            is_admin=False,
            manage_users_url=None,
        )

        self.assertIn("Workspace Adoption", html)
        self.assertEqual(html.count("0 users"), 3)

    @patch("bidlens.routes.integrations._microsoft_user_context")
    def test_microsoft_connection_route_renders_real_adoption_and_actions(self, user_context):
        self.user.current_role = "admin"
        self.db.add_all([
            OrganizationMembership(organization_id=self.org.id, user_id=self.user.id, role="admin"),
            OrganizationMembership(organization_id=self.org.id, user_id=self.other_user.id, role="member"),
            ExternalIntegrationConnection(
                workspace_id=self.workspace.id,
                user_id=self.user.id,
                provider=PROVIDER_MICROSOFT,
                connection_status=STATUS_CONNECTED,
                connected_at=dt.datetime(2026, 8, 1),
                last_verified_at=dt.datetime(2026, 8, 2),
            ),
        ])
        self.db.commit()
        user_context.return_value = (self.user, self.workspace, None)
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/integrations/microsoft",
            "root_path": "",
            "scheme": "http",
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        })

        response = asyncio.run(integrations.microsoft_connection_page(request, self.db))
        html = response.body.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("Workspace Adoption", html)
        self.assertIn("1 user", html)
        self.assertIn("Test Connection", html)
        self.assertIn("Disconnect", html)
        self.assertIn("Sync Now", html)
        self.assertIn(f"/admin/organizations/{self.org.id}/users", html)


if __name__ == "__main__":
    unittest.main()
