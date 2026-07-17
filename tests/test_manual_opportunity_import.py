import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import IngestionRun, Opportunity, Organization
from bidlens.routes import imports
from bidlens.services.manual_import import csv_template_text, import_manual_csv


class _Request:
    query_params = {"org_id": "7"}
    url = SimpleNamespace(query="org_id=7")


class ManualOpportunityImportTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        session_factory = sessionmaker(bind=self.engine)
        self.db = session_factory()
        self.org = Organization(name="Manual Import Org", slug="manual-import-org")
        self.db.add(self.org)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_template_download_is_generic_bidlens_csv(self):
        admin = SimpleNamespace(id=1, organization_id=self.org.id, current_organization_id=self.org.id, current_role="admin")

        with patch.object(imports, "require_admin", return_value=admin):
            response = asyncio.run(imports.manual_import_template(_Request(), db=MagicMock()))

        body = response.body.decode("utf-8")
        self.assertEqual(response.media_type, "text/csv")
        self.assertIn("source,source_record_id,title,agency,opportunity_type", body)
        self.assertIn("manual_import,manual-001,Example opportunity title", body)
        self.assertNotIn("GovWin Staging Name", body)
        self.assertNotIn("xlsx", body.lower())

    def test_manual_csv_import_creates_and_updates_by_source_record_id(self):
        csv_body = csv_template_text().replace(
            "Example opportunity title",
            "Initial manual opportunity",
        )
        first = import_manual_csv(self.db, self.org.id, csv_body.encode("utf-8"))
        self.db.commit()

        opportunity = self.db.query(Opportunity).one()
        self.assertEqual(first["created"], 1)
        self.assertEqual(opportunity.source, "manual_import")
        self.assertEqual(opportunity.source_record_id, "manual-001")
        self.assertEqual(opportunity.title, "Initial manual opportunity")

        updated_csv = csv_body.replace("Initial manual opportunity", "Updated manual opportunity")
        second = import_manual_csv(self.db, self.org.id, updated_csv.encode("utf-8"))
        self.db.commit()

        opportunities = self.db.query(Opportunity).all()
        self.assertEqual(len(opportunities), 1)
        self.assertEqual(second["updated"], 1)
        self.assertEqual(opportunities[0].title, "Updated manual opportunity")

    def test_manual_csv_import_validation_reports_missing_required_fields(self):
        csv_body = "source,source_record_id,title,agency,opportunity_type,posted_date,response_deadline\nmanual_import,,Missing ID,Agency,RFP,2026-07-01,2026-08-15\n"

        result = import_manual_csv(self.db, self.org.id, csv_body.encode("utf-8"))

        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["reason_counts"], {"missing_source_record_id": 1})
        self.assertEqual(self.db.query(Opportunity).count(), 0)

    def test_manual_import_run_label_is_source_neutral(self):
        run = imports._record_manual_import_run(
            self.db,
            organization_id=self.org.id,
            user_id=1,
            filename="manual.csv",
            result={"processed": 0, "created": 0, "updated": 0, "unchanged": 0, "skipped": 0, "errors": 0},
        )
        self.db.commit()

        self.assertEqual(run.source, "manual_import")
        self.assertEqual(imports._source_label(run.source), "Manual Import")
        self.assertEqual(self.db.query(IngestionRun).count(), 1)


if __name__ == "__main__":
    unittest.main()
