import asyncio
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import IngestionRun, Opportunity, Organization, OrgProfile, User
from bidlens.routes import integrations
from bidlens.services.govwin import GovWinAdapter
from bidlens.services.govwin_import import upsert_govwin_opportunity
from bidlens.services.integration_credentials import decrypt_credentials, encrypt_credentials


class GovWinAdapterTests(unittest.TestCase):
    def test_credentials_are_encrypted_and_round_trip(self):
        credentials = {
            "client_id": "client-value",
            "client_secret": "secret-value",
            "username": "user-value",
            "password": "password-value",
        }

        encrypted = encrypt_credentials(credentials)

        self.assertNotIn("client-value", encrypted)
        self.assertNotIn("password-value", encrypted)
        self.assertEqual(decrypt_credentials(encrypted), credentials)

    def test_adapter_normalizes_to_govwin_api_source(self):
        adapter = GovWinAdapter(
            {
                "client_id": "client",
                "client_secret": "secret",
                "username": "user",
                "password": "password",
            }
        )

        normalized = adapter.normalize_opportunity(adapter.sync_saved_search()[0])

        self.assertEqual(normalized["source"], "govwin_api")
        self.assertEqual(normalized["source_record_id"], "GW-MOCK-1001")
        self.assertEqual(normalized["govwin_staging_id"], "GW-MOCK-1001")


class GovWinIntegrationRouteTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        session_factory = sessionmaker(bind=self.engine)
        self.db = session_factory()

        self.org = Organization(name="Test Org", slug="govwin-integration-test")
        self.db.add(self.org)
        self.db.flush()
        self.user = User(email="admin@example.com", organization_id=self.org.id)
        self.db.add(self.user)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _save_credentials(self):
        with patch.object(
            integrations,
            "_admin_or_redirect",
            return_value=(self.user, None),
        ):
            return asyncio.run(
                integrations.save_govwin_configuration(
                    request=MagicMock(),
                    client_id="client",
                    client_secret="secret",
                    username="user",
                    password="password",
                    db=self.db,
                )
            )

    def test_configuration_save_stores_only_encrypted_credentials(self):
        self._save_credentials()

        profile = self.db.query(OrgProfile).filter(OrgProfile.org_id == self.org.id).one()

        self.assertNotIn("password", profile.govwin_credentials_encrypted)
        self.assertEqual(
            decrypt_credentials(profile.govwin_credentials_encrypted),
            {
                "client_id": "client",
                "client_secret": "secret",
                "username": "user",
                "password": "password",
            },
        )

    def test_mock_sync_upserts_and_records_source_activity(self):
        self._save_credentials()

        with patch.object(
            integrations,
            "_admin_or_redirect",
            return_value=(self.user, None),
        ):
            asyncio.run(integrations.run_govwin_sync(request=MagicMock(), db=self.db))
            asyncio.run(integrations.run_govwin_sync(request=MagicMock(), db=self.db))

        opportunities = (
            self.db.query(Opportunity)
            .filter(
                Opportunity.organization_id == self.org.id,
                Opportunity.source == "govwin_api",
            )
            .all()
        )
        runs = (
            self.db.query(IngestionRun)
            .filter(
                IngestionRun.organization_id == self.org.id,
                IngestionRun.source == "govwin_api",
            )
            .order_by(IngestionRun.id.asc())
            .all()
        )

        self.assertEqual(len(opportunities), 2)
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0].created_count, 2)
        self.assertEqual(runs[1].unchanged_count, 2)

    def test_existing_govwin_export_source_remains_idempotent(self):
        adapter = GovWinAdapter({})
        normalized = adapter.normalize_opportunity(adapter.sync_saved_search()[0])
        normalized["source"] = "govwin_export"
        normalized["source_record_id"] = "GovWin Staging Name 1001"

        first, _opp, _diagnostic, _reason = upsert_govwin_opportunity(
            self.db,
            self.org.id,
            normalized,
        )
        second, _opp, _diagnostic, _reason = upsert_govwin_opportunity(
            self.db,
            self.org.id,
            normalized,
        )

        count = (
            self.db.query(Opportunity)
            .filter(
                Opportunity.organization_id == self.org.id,
                Opportunity.source == "govwin_export",
                Opportunity.source_record_id == "GovWin Staging Name 1001",
            )
            .count()
        )
        self.assertEqual(first, "created")
        self.assertEqual(second, "unchanged")
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
