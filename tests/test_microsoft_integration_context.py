import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import ExternalIntegrationConnection, Organization, User, Workspace, WorkspaceIntegration
from bidlens.services.integration_credentials import encrypt_credentials
from bidlens.services.microsoft import (
    STATUS_CONNECTED,
    STATUS_DISCONNECTED,
    MicrosoftConnectionService,
    connection_status_summary,
)
from bidlens.services.microsoft_integration_context import resolve_microsoft_integration_context


class MicrosoftIntegrationContextTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

        self.org = Organization(name="Context Org", slug="context-org")
        self.other_org = Organization(name="Other Context Org", slug="other-context-org")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.workspace = Workspace(organization_id=self.org.id, name="Context", slug="context")
        self.other_workspace = Workspace(
            organization_id=self.other_org.id,
            name="Other Context",
            slug="other-context",
        )
        self.user = User(email="one@example.com", name="One", organization_id=self.org.id)
        self.other_user = User(email="two@example.com", name="Two", organization_id=self.org.id)
        self.foreign_user = User(email="foreign@example.com", name="Foreign", organization_id=self.other_org.id)
        self.db.add_all([self.workspace, self.other_workspace, self.user, self.other_user, self.foreign_user])
        self.db.flush()
        self.integration = WorkspaceIntegration(
            workspace_id=self.workspace.id,
            provider="microsoft",
            mode="individual",
            status="configured",
        )
        self.other_integration = WorkspaceIntegration(
            workspace_id=self.other_workspace.id,
            provider="microsoft",
            mode="individual",
            status="configured",
        )
        self.db.add_all([self.integration, self.other_integration])
        self.db.flush()
        self.connection = self._connection(self.integration, self.workspace, self.user, "tenant-a", "ms-one")
        self.other_connection = self._connection(
            self.integration,
            self.workspace,
            self.other_user,
            "tenant-b",
            "ms-two",
        )
        self.foreign_connection = self._connection(
            self.other_integration,
            self.other_workspace,
            self.foreign_user,
            "tenant-a",
            "ms-foreign",
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _connection(self, integration, workspace, user, tenant_id, external_user_id):
        connection = ExternalIntegrationConnection(
            workspace_integration_id=integration.id,
            workspace_id=workspace.id,
            user_id=user.id,
            provider="microsoft",
            connection_status=STATUS_CONNECTED,
            external_tenant_id=tenant_id,
            external_user_id=external_user_id,
            encrypted_access_token=encrypt_credentials({"token": external_user_id}),
            encrypted_refresh_token=encrypt_credentials({"token": f"refresh-{external_user_id}"}),
            granted_scopes="Mail.Send Mail.ReadWrite",
        )
        self.db.add(connection)
        return connection

    def test_parent_and_current_user_connection_resolve_together(self):
        context = resolve_microsoft_integration_context(
            self.db,
            workspace=self.workspace,
            current_user=self.user,
        )

        self.assertEqual(context.workspace_integration.id, self.integration.id)
        self.assertEqual(context.current_user_connection.id, self.connection.id)
        self.assertEqual(context.workspace_id, self.workspace.id)
        self.assertEqual(context.provider, "microsoft")
        self.assertEqual(context.mode, "individual")
        self.assertEqual(context.status, "configured")
        self.assertTrue(context.is_configured)
        self.assertTrue(context.is_individual_mode)
        self.assertTrue(context.has_current_user_connection)

    def test_parent_without_current_user_connection_does_not_borrow_another_member(self):
        unconnected = User(email="none@example.com", name="None", organization_id=self.org.id)
        self.db.add(unconnected)
        self.db.commit()

        context = resolve_microsoft_integration_context(
            self.db,
            workspace=self.workspace,
            current_user=unconnected,
        )

        self.assertEqual(context.workspace_integration.id, self.integration.id)
        self.assertIsNone(context.current_user_connection)
        self.assertTrue(context.is_configured)
        self.assertFalse(context.has_current_user_connection)

    def test_each_member_receives_only_their_own_connection(self):
        first = resolve_microsoft_integration_context(
            self.db,
            workspace=self.workspace,
            current_user=self.user,
        )
        second = resolve_microsoft_integration_context(
            self.db,
            workspace=self.workspace,
            current_user=self.other_user,
        )

        self.assertEqual(first.workspace_integration.id, second.workspace_integration.id)
        self.assertEqual(first.current_user_connection.id, self.connection.id)
        self.assertEqual(second.current_user_connection.id, self.other_connection.id)
        self.assertNotEqual(first.current_user_connection.id, second.current_user_connection.id)

    def test_workspace_scope_prevents_foreign_connection_resolution(self):
        context = resolve_microsoft_integration_context(
            self.db,
            workspace=self.workspace,
            current_user=self.foreign_user,
        )

        self.assertEqual(context.workspace_integration.id, self.integration.id)
        self.assertIsNone(context.current_user_connection)

    def test_legacy_child_without_parent_is_read_as_individual_compatibility_state(self):
        legacy_org = Organization(name="Legacy Org", slug="legacy-context-org")
        self.db.add(legacy_org)
        self.db.flush()
        legacy_workspace = Workspace(organization_id=legacy_org.id, name="Legacy", slug="legacy-context")
        legacy_user = User(email="legacy@example.com", name="Legacy", organization_id=legacy_org.id)
        self.db.add_all([legacy_workspace, legacy_user])
        self.db.flush()
        legacy_connection = ExternalIntegrationConnection(
            workspace_id=legacy_workspace.id,
            user_id=legacy_user.id,
            provider="microsoft",
            connection_status=STATUS_CONNECTED,
        )
        self.db.add(legacy_connection)
        self.db.commit()

        context = resolve_microsoft_integration_context(
            self.db,
            workspace=legacy_workspace,
            current_user=legacy_user,
        )

        self.assertIsNone(context.workspace_integration)
        self.assertEqual(context.current_user_connection.id, legacy_connection.id)
        self.assertEqual(context.mode, "individual")
        self.assertEqual(context.status, "configured")

    def test_existing_connection_status_summary_is_unchanged(self):
        context = resolve_microsoft_integration_context(
            self.db,
            workspace=self.workspace,
            current_user=self.user,
        )

        self.assertEqual(
            connection_status_summary(context.current_user_connection),
            connection_status_summary(self.connection),
        )

    def test_disconnecting_one_member_preserves_parent_and_other_member(self):
        MicrosoftConnectionService(
            db=self.db,
            workspace=self.workspace,
            user=self.user,
        ).disconnect()
        self.db.commit()

        self.assertEqual(self.integration.status, "configured")
        self.assertEqual(self.connection.connection_status, STATUS_DISCONNECTED)
        self.assertEqual(self.other_connection.connection_status, STATUS_CONNECTED)
        other_context = resolve_microsoft_integration_context(
            self.db,
            workspace=self.workspace,
            current_user=self.other_user,
        )
        self.assertEqual(other_context.current_user_connection.id, self.other_connection.id)


if __name__ == "__main__":
    unittest.main()
