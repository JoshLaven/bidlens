import asyncio
import base64
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi.responses import RedirectResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from bidlens.database import Base
from bidlens.models import ExternalIntegrationConnection, Organization, User, Workspace, WorkspaceIntegration
from bidlens.routes import integrations
from bidlens.services.integration_credentials import decrypt_credentials, encrypt_credentials
from bidlens.services.microsoft import MicrosoftConnectionError, MicrosoftConnectionService
from bidlens.services.microsoft_integration_context import resolve_microsoft_integration_context
from bidlens.services.microsoft_workspace_configuration import (
    MicrosoftWorkspaceConfigurationError,
    MicrosoftWorkspaceConfigurationService,
)


def _id_token(*, user_id: str, tenant_id: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"oid": user_id, "tid": tenant_id}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def _graph_response(*, user_id: str, email: str):
    response = Mock()
    response.ok = True
    response.json.return_value = {
        "id": user_id,
        "displayName": user_id,
        "userPrincipalName": email,
    }
    return response


class MicrosoftWorkspaceConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.org = Organization(name="Managed Org", slug="managed-org")
        self.other_org = Organization(name="Other Managed Org", slug="other-managed-org")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.workspace = Workspace(organization_id=self.org.id, name="Managed", slug="managed")
        self.other_workspace = Workspace(organization_id=self.other_org.id, name="Other", slug="other-managed")
        self.admin = User(email="admin@example.com", name="Admin", organization_id=self.org.id)
        self.member = User(email="member@example.com", name="Member", organization_id=self.org.id)
        self.db.add_all([self.workspace, self.other_workspace, self.admin, self.member])
        self.db.flush()
        self.integration = WorkspaceIntegration(
            workspace_id=self.workspace.id,
            provider="microsoft",
            mode="individual",
            status="configured",
        )
        self.db.add(self.integration)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _configuration(self):
        return MicrosoftWorkspaceConfigurationService(db=self.db, workspace=self.workspace)

    def _connection(self, user, tenant_id, *, access_token=None):
        connection = ExternalIntegrationConnection(
            workspace_integration_id=self.integration.id,
            workspace_id=self.workspace.id,
            user_id=user.id,
            provider="microsoft",
            connection_status="connected",
            external_tenant_id=tenant_id,
            external_user_id=f"ms-{user.id}",
            encrypted_access_token=encrypt_credentials({"token": access_token or f"access-{user.id}"}),
            encrypted_refresh_token=encrypt_credentials({"token": f"refresh-{user.id}"}),
        )
        self.db.add(connection)
        self.db.commit()
        return connection

    def test_individual_mode_permits_different_member_tenants(self):
        first = self._connection(self.admin, "tenant-a")
        second = self._connection(self.member, "tenant-b")

        service = self._configuration()
        service.validate_member_tenant(first.external_tenant_id)
        service.validate_member_tenant(second.external_tenant_id)
        self.assertEqual(service.distinct_member_tenant_ids(), ("tenant-a", "tenant-b"))

    def test_organization_mode_requires_explicit_authoritative_tenant(self):
        with self.assertRaises(MicrosoftWorkspaceConfigurationError) as context:
            self._configuration().set_connection_mode("organization")

        self.assertEqual(context.exception.code, "organization_tenant_required")
        self.db.refresh(self.integration)
        self.assertEqual(self.integration.mode, "individual")
        self.assertIsNone(self.integration.external_tenant_id)

    def test_mixed_tenants_are_reported_and_not_auto_resolved(self):
        first = self._connection(self.admin, "tenant-a")
        second = self._connection(self.member, "tenant-b")

        conflicts = self._configuration().organization_mode_conflicts("tenant-a")
        self.assertEqual(conflicts.connection_ids, (second.id,))
        with self.assertRaises(MicrosoftWorkspaceConfigurationError) as context:
            self._configuration().set_connection_mode("organization", external_tenant_id="tenant-a")

        self.assertEqual(context.exception.code, "organization_tenant_conflict")
        self.assertEqual(context.exception.conflict_count, 1)
        self.assertEqual(first.external_tenant_id, "tenant-a")
        self.assertEqual(second.external_tenant_id, "tenant-b")
        self.assertEqual(self.integration.mode, "individual")

    def test_unverified_existing_connection_is_a_conflict(self):
        connection = self._connection(self.admin, None)
        conflicts = self._configuration().organization_mode_conflicts("tenant-a")
        self.assertEqual(conflicts.connection_ids, (connection.id,))

    def test_organization_to_individual_clears_policy_but_preserves_connections(self):
        connection = self._connection(self.admin, "tenant-a")
        service = self._configuration()
        service.set_connection_mode(
            "organization",
            external_tenant_id="TENANT-A",
            tenant_display_name="Managed Tenant",
        )
        service.set_connection_mode("individual")
        self.db.commit()

        self.assertEqual(self.integration.mode, "individual")
        self.assertIsNone(self.integration.external_tenant_id)
        self.assertIsNone(self.integration.tenant_display_name)
        self.assertEqual(self.db.get(ExternalIntegrationConnection, connection.id).external_tenant_id, "tenant-a")

    def test_workspace_isolation_excludes_other_workspace_connections(self):
        other_integration = WorkspaceIntegration(
            workspace_id=self.other_workspace.id,
            provider="microsoft",
            mode="individual",
            status="configured",
        )
        self.db.add(other_integration)
        self.db.flush()
        foreign_connection = ExternalIntegrationConnection(
            workspace_integration_id=other_integration.id,
            workspace_id=self.other_workspace.id,
            user_id=self.admin.id,
            provider="microsoft",
            external_tenant_id="foreign-tenant",
        )
        self.db.add(foreign_connection)
        self.db.commit()

        self._configuration().set_connection_mode("organization", external_tenant_id="tenant-a")
        self.assertEqual(self.integration.external_tenant_id, "tenant-a")

    @patch("bidlens.services.microsoft.requests.get")
    def test_same_tenant_delegated_connection_succeeds_in_organization_mode(self, mock_get):
        self._configuration().set_connection_mode("organization", external_tenant_id="tenant-a")
        mock_get.return_value = _graph_response(user_id="ms-admin", email=self.admin.email)

        connection = MicrosoftConnectionService(
            db=self.db,
            workspace=self.workspace,
            user=self.admin,
        ).complete_connection({
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "id_token": _id_token(user_id="ms-admin", tenant_id="TENANT-A"),
        })

        self.assertEqual(connection.external_tenant_id, "TENANT-A")
        self.assertEqual(connection.workspace_integration_id, self.integration.id)

    @patch("bidlens.services.microsoft.requests.get")
    def test_different_tenant_reconnect_is_rejected_without_overwrite(self, mock_get):
        existing = self._connection(self.admin, "tenant-a", access_token="keep-access")
        self._configuration().set_connection_mode("organization", external_tenant_id="tenant-a")
        self.db.commit()
        mock_get.return_value = _graph_response(user_id=existing.external_user_id, email=self.admin.email)

        with self.assertRaises(MicrosoftConnectionError) as context:
            MicrosoftConnectionService(
                db=self.db,
                workspace=self.workspace,
                user=self.admin,
            ).complete_connection({
                "access_token": "reject-access",
                "refresh_token": "reject-refresh",
                "id_token": _id_token(user_id=existing.external_user_id, tenant_id="tenant-b"),
            })

        self.assertEqual(context.exception.code, "organization_tenant_mismatch")
        self.db.refresh(existing)
        self.assertEqual(existing.external_tenant_id, "tenant-a")
        self.assertEqual(decrypt_credentials(existing.encrypted_access_token)["token"], "keep-access")

    def test_context_reports_organization_tenant(self):
        self._configuration().set_connection_mode(
            "organization",
            external_tenant_id="tenant-a",
            tenant_display_name="Managed Tenant",
        )
        context = resolve_microsoft_integration_context(
            self.db,
            workspace=self.workspace,
            current_user=self.admin,
        )

        self.assertTrue(context.is_organization_mode)
        self.assertFalse(context.is_individual_mode)
        self.assertEqual(context.external_tenant_id, "tenant-a")
        self.assertEqual(context.tenant_display_name, "Managed Tenant")

    def test_graph_token_access_rechecks_organization_tenant_policy(self):
        connection = self._connection(self.admin, "tenant-b")
        self.integration.mode = "organization"
        self.integration.external_tenant_id = "tenant-a"
        self.db.commit()

        with self.assertRaises(MicrosoftConnectionError) as context:
            MicrosoftConnectionService(
                db=self.db,
                workspace=self.workspace,
                user=self.admin,
            ).access_token_for_connection(connection)

        self.assertEqual(context.exception.code, "organization_tenant_mismatch")

    @patch("bidlens.routes.integrations._admin_or_redirect")
    def test_non_admin_cannot_change_mode(self, admin_check):
        admin_check.return_value = (None, RedirectResponse(url="/", status_code=303))
        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/integrations/microsoft/mode",
            "headers": [],
            "query_string": b"",
        })

        response = asyncio.run(integrations.configure_microsoft_connection_mode(
            request,
            mode="organization",
            external_tenant_id="tenant-a",
            tenant_display_name="Managed",
            db=self.db,
        ))

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/")
        self.assertEqual(self.integration.mode, "individual")

    @patch("bidlens.routes.integrations._current_workspace")
    @patch("bidlens.routes.integrations._admin_or_redirect")
    def test_admin_can_configure_organization_mode(self, admin_check, current_workspace):
        self.admin.current_role = "admin"
        admin_check.return_value = (self.admin, None)
        current_workspace.return_value = self.workspace
        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/integrations/microsoft/mode",
            "headers": [],
            "query_string": b"",
        })

        response = asyncio.run(integrations.configure_microsoft_connection_mode(
            request,
            mode="organization",
            external_tenant_id="tenant-a",
            tenant_display_name="Managed Tenant",
            db=self.db,
        ))

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/integrations/microsoft?mode_saved=1")
        self.db.refresh(self.integration)
        self.assertEqual(self.integration.mode, "organization")
        self.assertEqual(self.integration.external_tenant_id, "tenant-a")

    def test_organization_mode_template_is_accurate_and_admin_only_tenant_id(self):
        self._configuration().set_connection_mode(
            "organization",
            external_tenant_id="tenant-a",
            tenant_display_name="Managed Tenant",
        )
        context = resolve_microsoft_integration_context(self.db, workspace=self.workspace)
        base_context = {
            "request": SimpleNamespace(query_params={}),
            "workspace": self.workspace,
            "microsoft_context": context,
            "status": {"connected": False, "status": "not_connected", "level": "neutral", "label": "Not connected"},
            "adoption": {"connected": 0, "not_connected": 1, "total": 1},
            "scope_summary": "Mail.Send, Mail.ReadWrite",
            "manage_users_url": None,
        }
        template = integrations.templates.env.get_template("microsoft_connection.html")
        admin_html = template.render(**base_context, is_admin=True)
        member_html = template.render(**base_context, is_admin=False)

        self.assertIn("Organization-managed", admin_html)
        self.assertIn("Managed Tenant", admin_html)
        self.assertIn("tenant-a", admin_html)
        self.assertIn("Member mailbox access still uses delegated Microsoft identities", admin_html)
        self.assertNotIn("Save Connection Mode", member_html)
        self.assertNotIn("<dt>Tenant ID</dt>", member_html)


if __name__ == "__main__":
    unittest.main()
