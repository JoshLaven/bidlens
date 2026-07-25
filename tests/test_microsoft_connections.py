import base64
import datetime as dt
import json
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import (
    Event,
    ExternalIntegrationConnection,
    ExternalIntegrationOAuthState,
    Organization,
    User,
    Workspace,
)
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


if __name__ == "__main__":
    unittest.main()
