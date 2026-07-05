import unittest
from datetime import date
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.ingest_grants_gov import ingest_grants_gov
from bidlens.ingest_sam import ingest_sam
from bidlens.models import IngestionRunDetail, OpportunityUpdateEvent, Organization
from bidlens.services.govwin_import import (
    _normalize_row,
    import_govwin_xlsx,
    upsert_govwin_opportunity,
)
from bidlens.services.ingestion_runs import record_source_activity


class IngestionRunDetailTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Import Audit", slug="import-audit")
        self.db.add(self.org)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def _row(source_record_id, title):
        return {
            "Title": title,
            "GovWin Staging Name": source_record_id,
            "GovEntity Title": "Department of Health and Human Services",
            "Created Date": date(2026, 7, 1),
            "Response Date": date(2026, 8, 1),
            "Solicitation Number": f"SOL-{source_record_id}",
            "GW Description": f"Description for {title}",
        }

    def _persist_result(self, result):
        run = record_source_activity(
            self.db,
            source="govwin_export",
            organization_id=self.org.id,
            user_id=None,
            filename="audit.xlsx",
            result=result,
        )
        self.db.commit()
        return run

    def test_govwin_run_persists_created_updated_unchanged_and_skipped_details(self):
        unchanged_row = self._row("existing-same", "Existing unchanged")
        unchanged_data, _ = _normalize_row(unchanged_row, 2)
        upsert_govwin_opportunity(self.db, self.org.id, unchanged_data)

        old_update_row = self._row("existing-update", "Old title")
        old_update_data, _ = _normalize_row(old_update_row, 3)
        upsert_govwin_opportunity(self.db, self.org.id, old_update_data)
        self.db.commit()

        new_row = self._row("new-record", "New opportunity")
        rows = [
            new_row,
            unchanged_row,
            self._row("existing-update", "Revised title"),
            dict(new_row),
            self._row("invalid-record", None),
        ]
        with patch(
            "bidlens.services.govwin_import.parse_xlsx_rows",
            return_value=rows,
        ):
            result = import_govwin_xlsx(self.db, self.org.id, b"mock workbook")

        run = self._persist_result(result)
        details = (
            self.db.query(IngestionRunDetail)
            .filter(IngestionRunDetail.ingestion_run_id == run.id)
            .order_by(IngestionRunDetail.id.asc())
            .all()
        )

        self.assertEqual(
            [detail.result for detail in details],
            ["created", "unchanged", "updated", "skipped_duplicate", "skipped_invalid"],
        )
        self.assertEqual(details[2].changed_fields_json.keys(), {"title", "description", "description_text"})
        self.assertIn("Duplicate row within same import file", details[3].reason)
        self.assertEqual(run.processed_count, 5)
        self.assertEqual(run.created_count, 1)
        self.assertEqual(run.updated_count, 1)
        self.assertEqual(run.unchanged_count, 1)
        self.assertEqual(run.skipped_count, 2)
        update_event = self.db.query(OpportunityUpdateEvent).one()
        self.assertEqual(update_event.ingestion_run_id, run.id)
        self.assertEqual(update_event.salesforce_sync_status, "not_linked")

    def test_import_exception_persists_error_detail(self):
        row = self._row("error-record", "Broken opportunity")
        with (
            patch(
                "bidlens.services.govwin_import.parse_xlsx_rows",
                return_value=[row],
            ),
            patch(
                "bidlens.services.govwin_import.upsert_govwin_opportunity",
                side_effect=RuntimeError("simulated row failure"),
            ),
        ):
            result = import_govwin_xlsx(self.db, self.org.id, b"mock workbook")

        run = self._persist_result(result)
        detail = (
            self.db.query(IngestionRunDetail)
            .filter(IngestionRunDetail.ingestion_run_id == run.id)
            .one()
        )

        self.assertEqual(detail.result, "error")
        self.assertEqual(detail.error_message, "simulated row failure")
        self.assertIn("Import failed", detail.reason)
        self.assertEqual(run.error_count, 1)

    def test_govwin_stage_mapping_and_source_selection_skip(self):
        expected = {
            "Forecast Pre-RFP": "Forecast",
            "Pre-RFP": "RFI",
            "Post-RFP": "RFP",
        }
        for source_stage, display_stage in expected.items():
            row = self._row(f"stage-{display_stage}", f"{display_stage} opportunity")
            row["Status"] = source_stage
            row["Type"] = "Legacy Type Value"
            normalized, reason = _normalize_row(row, 2)
            self.assertIsNone(reason)
            self.assertEqual(normalized["source_stage"], source_stage)
            self.assertEqual(normalized["opportunity_type"], display_stage)

        source_selection = self._row("source-selection", "Award in review")
        source_selection["Type"] = "Source Selection"
        with patch(
            "bidlens.services.govwin_import.parse_xlsx_rows",
            return_value=[source_selection],
        ):
            result = import_govwin_xlsx(self.db, self.org.id, b"mock workbook")

        self.assertEqual(result["created"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["reason_counts"], {"source_selection": 1})

    def test_sam_pull_persists_one_detail_per_processed_record(self):
        record = {
            "noticeId": "sam-detail-1",
            "title": "SAM detail opportunity",
            "department": "Department of Health and Human Services",
            "type": "Solicitation",
            "postedDate": "2026-07-01",
            "responseDeadLine": "2026-08-01",
            "uiLink": "https://sam.gov/opp/sam-detail-1",
        }
        with patch(
            "bidlens.ingest_sam.search_opportunities",
            side_effect=[
                {"opportunitiesData": [record]},
                {"opportunitiesData": []},
            ],
        ):
            result = ingest_sam(
                self.db,
                organization_id=self.org.id,
                naics_list=["541611"],
                manual_pull=True,
            )

        details = (
            self.db.query(IngestionRunDetail)
            .filter(IngestionRunDetail.ingestion_run_id == result["run_id"])
            .all()
        )
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0].source, "sam.gov")
        self.assertEqual(details[0].result, "created")

    def test_grants_pull_supplies_details_to_source_activity(self):
        record = {
            "id": "grant-detail-1",
            "title": "Grant detail opportunity",
            "agency": "National Institutes of Health",
            "postedDate": "2026-07-01",
            "closeDate": "2026-08-01",
        }
        with (
            patch("bidlens.ingest_grants_gov.search_recent_opportunities", return_value={}),
            patch("bidlens.ingest_grants_gov._extract_records", return_value=[record]),
            patch("bidlens.ingest_grants_gov.fetch_opportunity_detail", return_value={}),
        ):
            result = ingest_grants_gov(self.db, organization_id=self.org.id)

        run = self._persist_result(result)
        detail = (
            self.db.query(IngestionRunDetail)
            .filter(IngestionRunDetail.ingestion_run_id == run.id)
            .one()
        )
        self.assertEqual(detail.source, "grants.gov")
        self.assertEqual(detail.source_record_id, "grant-detail-1")
        self.assertEqual(detail.result, "created")


if __name__ == "__main__":
    unittest.main()
