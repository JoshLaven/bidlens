import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, MetaData, String, Table, Text, UniqueConstraint, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import ExternalIntegrationConnection, Organization, User, Workspace, WorkspaceIntegration
from bidlens.services.integration_credentials import decrypt_credentials
from bidlens.services.microsoft import MicrosoftConnectionService


MIGRATION_PATH = Path("alembic/versions/c3d4e5f6a7b9_add_workspace_integrations.py")


def _load_migration():
    spec = importlib.util.spec_from_file_location("workspace_integration_migration", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _graph_response(*, user_id: str, email: str):
    response = Mock()
    response.ok = True
    response.json.return_value = {
        "id": user_id,
        "displayName": user_id,
        "userPrincipalName": email,
    }
    return response


class WorkspaceIntegrationModelTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.org = Organization(name="Integration Org", slug="integration-org")
        self.other_org = Organization(name="Other Org", slug="other-integration-org")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.workspace = Workspace(organization_id=self.org.id, name="Workspace", slug="integration-workspace")
        self.other_workspace = Workspace(
            organization_id=self.other_org.id,
            name="Other Workspace",
            slug="other-integration-workspace",
        )
        self.users = [
            User(email="one@example.com", name="One", organization_id=self.org.id),
            User(email="two@example.com", name="Two", organization_id=self.org.id),
        ]
        self.db.add_all([self.workspace, self.other_workspace, *self.users])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @patch("bidlens.services.microsoft.config.MICROSOFT_CLIENT_ID", "client")
    @patch("bidlens.services.microsoft.config.MICROSOFT_CLIENT_SECRET", "secret")
    @patch("bidlens.services.microsoft.config.MICROSOFT_REDIRECT_URI", "https://app.example.com/callback")
    @patch("bidlens.services.microsoft.requests.get")
    def test_delegated_connections_share_one_individual_workspace_integration(self, mock_get):
        connections = []
        for index, user in enumerate(self.users, start=1):
            mock_get.return_value = _graph_response(user_id=f"ms-user-{index}", email=user.email)
            connection = MicrosoftConnectionService(
                db=self.db,
                workspace=self.workspace,
                user=user,
            ).complete_connection({
                "access_token": f"access-{index}",
                "refresh_token": f"refresh-{index}",
                "id_token": _id_token(user_id=f"ms-user-{index}", tenant_id=f"tenant-{index}"),
            })
            connections.append(connection)
        self.db.commit()

        integration = self.db.query(WorkspaceIntegration).one()
        self.assertEqual(integration.workspace_id, self.workspace.id)
        self.assertEqual(integration.provider, "microsoft")
        self.assertEqual(integration.mode, "individual")
        self.assertEqual(integration.status, "configured")
        self.assertEqual({row.workspace_integration_id for row in connections}, {integration.id})
        self.assertEqual({row.external_tenant_id for row in connections}, {"tenant-1", "tenant-2"})
        self.assertEqual(len(integration.member_connections), 2)
        self.assertEqual(decrypt_credentials(connections[0].encrypted_access_token)["token"], "access-1")

    def test_workspace_provider_uniqueness_and_isolation(self):
        self.db.add(WorkspaceIntegration(
            workspace_id=self.workspace.id,
            provider="microsoft",
            mode="individual",
            status="configured",
        ))
        self.db.commit()
        self.db.add(WorkspaceIntegration(
            workspace_id=self.workspace.id,
            provider="microsoft",
            mode="individual",
            status="configured",
        ))
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

        self.db.add(WorkspaceIntegration(
            workspace_id=self.other_workspace.id,
            provider="microsoft",
            mode="individual",
            status="configured",
        ))
        self.db.commit()
        self.assertEqual(self.db.query(WorkspaceIntegration).count(), 2)
        self.assertEqual(len(self.workspace.workspace_integrations), 1)

    def test_mode_and_status_are_controlled(self):
        self.db.add(WorkspaceIntegration(
            workspace_id=self.workspace.id,
            provider="microsoft",
            mode="tenant_magic",
            status="configured",
        ))
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

        self.db.add(WorkspaceIntegration(
            workspace_id=self.workspace.id,
            provider="microsoft",
            mode="individual",
            status="mystery",
        ))
        with self.assertRaises(IntegrityError):
            self.db.commit()


class WorkspaceIntegrationMigrationTests(unittest.TestCase):
    def test_migration_backfills_one_parent_without_changing_credentials_or_identity(self):
        module = _load_migration()
        self.assertEqual(module.down_revision, "b2c3d4e5f6a8")
        with tempfile.NamedTemporaryFile(suffix=".db") as db_file:
            engine = create_engine(f"sqlite:///{db_file.name}")
            metadata = MetaData()
            Table("workspaces", metadata, Column("id", Integer, primary_key=True))
            Table("users", metadata, Column("id", Integer, primary_key=True))
            Table(
                "external_integration_connections",
                metadata,
                Column("id", Integer, primary_key=True),
                Column("workspace_id", Integer, nullable=False),
                Column("user_id", Integer, nullable=False),
                Column("provider", String, nullable=False),
                Column("external_tenant_id", String),
                Column("external_user_id", String),
                Column("encrypted_access_token", Text),
                Column("encrypted_refresh_token", Text),
                UniqueConstraint("workspace_id", "user_id", "provider"),
            )
            metadata.create_all(engine)
            with engine.begin() as connection:
                connection.execute(text("INSERT INTO workspaces (id) VALUES (1), (2)"))
                connection.execute(text("INSERT INTO users (id) VALUES (10), (11)"))
                connection.execute(text("""
                    INSERT INTO external_integration_connections
                        (id, workspace_id, user_id, provider, external_tenant_id, external_user_id,
                         encrypted_access_token, encrypted_refresh_token)
                    VALUES
                        (1, 1, 10, 'microsoft', 'tenant-a', 'user-a', 'access-a', 'refresh-a'),
                        (2, 1, 11, 'microsoft', 'tenant-b', 'user-b', 'access-b', 'refresh-b')
                """))
                module.op = Operations(MigrationContext.configure(connection))
                module.upgrade()
                parents = connection.execute(text(
                    "SELECT id, workspace_id, provider, mode, status FROM workspace_integrations"
                )).mappings().all()
                children = connection.execute(text("""
                    SELECT workspace_integration_id, external_tenant_id, external_user_id,
                           encrypted_access_token, encrypted_refresh_token
                    FROM external_integration_connections ORDER BY id
                """)).mappings().all()

            self.assertEqual(len(parents), 1)
            self.assertEqual(dict(parents[0]) | {}, {
                "id": parents[0]["id"],
                "workspace_id": 1,
                "provider": "microsoft",
                "mode": "individual",
                "status": "configured",
            })
            self.assertEqual({row["workspace_integration_id"] for row in children}, {parents[0]["id"]})
            self.assertEqual([row["external_tenant_id"] for row in children], ["tenant-a", "tenant-b"])
            self.assertEqual([row["external_user_id"] for row in children], ["user-a", "user-b"])
            self.assertEqual([row["encrypted_access_token"] for row in children], ["access-a", "access-b"])
            self.assertEqual([row["encrypted_refresh_token"] for row in children], ["refresh-a", "refresh-b"])
            self.assertIn("workspace_integration_id", {column["name"] for column in inspect(engine).get_columns(
                "external_integration_connections"
            )})
            engine.dispose()


def _id_token(*, user_id: str, tenant_id: str) -> str:
    import base64
    import json

    payload = base64.urlsafe_b64encode(json.dumps({"oid": user_id, "tid": tenant_id}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"
