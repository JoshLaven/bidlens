import asyncio
from datetime import datetime, timedelta
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import (
    GrantsSourceConfig,
    IngestionRun,
    IngestionRunDetail,
    Opportunity,
    Organization,
    OrgProfile,
    SamSourceConfig,
    User,
)
from bidlens.routes import integrations
from bidlens.services.govwin import GovWinAdapter
from bidlens.services.govwin_import import upsert_govwin_opportunity
from bidlens.services.integration_credentials import decrypt_credentials, encrypt_credentials
from bidlens.services.salesforce import SalesforceService


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
        details = (
            self.db.query(IngestionRunDetail)
            .filter(IngestionRunDetail.ingestion_run_id.in_([run.id for run in runs]))
            .order_by(IngestionRunDetail.id.asc())
            .all()
        )
        self.assertEqual(
            [detail.result for detail in details],
            ["created", "created", "unchanged", "unchanged"],
        )

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

    def test_configuration_center_exposes_tenant_scoped_connector_state(self):
        other_org = Organization(name="Other Org", slug="other-config-center")
        self.db.add(other_org)
        self.db.flush()
        self.db.add_all([
            GrantsSourceConfig(
                organization_id=self.org.id,
                enabled=True,
                posted_days_back=7,
                rows=25,
            ),
            SamSourceConfig(
                organization_id=self.org.id,
                name="Health Search",
                naics_codes=["541611", "541690"],
                notice_types=["Solicitation", "Sources Sought"],
            ),
            SamSourceConfig(
                organization_id=self.org.id,
                name="Research Search",
                naics_codes=["541690", "541720"],
                notice_types=["Presolicitation"],
            ),
            SamSourceConfig(
                organization_id=other_org.id,
                name="Other Tenant Search",
                naics_codes=["111110"],
                notice_types=["Solicitation"],
            ),
        ])
        successful_at = datetime(2026, 7, 4, 8, 0)
        self.db.add_all([
            IngestionRun(
                organization_id=self.org.id,
                source="sam.gov",
                status="success",
                started_at=successful_at,
                finished_at=successful_at + timedelta(minutes=2),
                error_count=0,
            ),
            IngestionRun(
                organization_id=self.org.id,
                source="sam.gov",
                status="paused_rate_limit",
                started_at=datetime(2026, 7, 5, 8, 0),
                finished_at=None,
                retry_after_at=datetime(2026, 7, 5, 10, 0),
                error_count=1,
            ),
            IngestionRun(
                organization_id=self.org.id,
                source="govwin_export",
                status="completed",
                started_at=datetime(2026, 7, 5, 9, 0),
                finished_at=datetime(2026, 7, 5, 9, 1),
                error_count=0,
                filename="GovWin export.xlsx",
            ),
            IngestionRun(
                organization_id=other_org.id,
                source="sam.gov",
                status="success",
                started_at=datetime(2026, 7, 6, 8, 0),
                finished_at=datetime(2026, 7, 6, 8, 1),
                error_count=0,
            ),
        ])
        self.db.commit()

        with patch.object(integrations.config, "SAM_API_KEY", "configured-key"):
            center = integrations._configuration_center_context(
                self.db,
                organization_id=self.org.id,
                profile=None,
                salesforce_snapshot={
                    "connected": True,
                    "instance_url": "https://example.my.salesforce.com",
                    "inspection": {
                        "required_fields_verified": True,
                        "default_stage_valid": True,
                        "selected_intake_source": "BidLens",
                        "field_mappings_valid": True,
                    },
                    "error": None,
                },
                now=datetime(2026, 7, 5, 12, 0),
            )

        self.assertTrue(center["sam"]["connected"])
        self.assertEqual(
            [source_config.name for source_config in center["sam"]["configs"]],
            ["Health Search", "Research Search"],
        )
        self.assertEqual(center["sam"]["naics_count"], 3)
        self.assertEqual(
            center["sam"]["notice_types"],
            ["Solicitation", "Sources Sought", "Presolicitation"],
        )
        self.assertEqual(center["sam"]["status"], {"label": "Paused", "tone": "paused"})
        self.assertEqual(center["sam"]["last_success"].started_at, successful_at)
        self.assertEqual(center["govwin"]["latest"].filename, "GovWin export.xlsx")
        self.assertEqual(center["salesforce"]["connection_health"], "Authorized")
        self.assertEqual(
            center["salesforce"]["instance_url"],
            "https://example.my.salesforce.com",
        )
        self.assertTrue(center["grants"]["connected"])
        self.assertEqual(center["sam"]["health"]["label"], "Paused")
        self.assertEqual(center["govwin"]["health"]["label"], "Healthy")
        self.assertEqual(center["salesforce"]["health"]["label"], "Healthy")
        self.assertEqual(
            center["overall_health"],
            {"total": 4, "healthy": 2, "paused": 1, "attention": 1},
        )

    def test_salesforce_requirement_inspection_reuses_single_describe(self):
        service = SalesforceService(
            instance_url="https://example.my.salesforce.com",
            client_id="client",
            client_secret="secret",
        )
        fields = [
            {
                "name": name,
                "createable": True,
                "nillable": name not in {"Name", "StageName", "CloseDate"},
                "defaultedOnCreate": False,
                "picklistValues": (
                    [{"active": True, "value": "Prospecting"}]
                    if name == "StageName"
                    else [{"active": True, "value": "BidLens"}]
                    if name == "Intake_Source_c__c"
                    else []
                ),
            }
            for name in (
                "Name",
                "StageName",
                "CloseDate",
                "External_Source_ID_c__c",
                "Intake_Status__c",
                "Intake_Source_c__c",
            )
        ]
        service.describe_opportunity = MagicMock(return_value={"fields": fields})

        inspection = service.inspect_opportunity_requirements()

        service.describe_opportunity.assert_called_once_with()
        self.assertTrue(inspection["default_stage_valid"])
        self.assertTrue(inspection["required_fields_verified"])
        self.assertTrue(inspection["field_mappings_valid"])
        self.assertEqual(inspection["selected_intake_source"], "BidLens")


if __name__ == "__main__":
    unittest.main()
