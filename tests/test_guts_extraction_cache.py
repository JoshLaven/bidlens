import hashlib
import io
import logging
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import (
    Opportunity,
    OpportunitySourceMaterial,
    OpportunitySourceMaterialExtraction,
    Organization,
    Workspace,
)
from bidlens.services.opportunity_intake.document_parsing import (
    IntakeDocumentError,
    ParsedIntakeDocument,
    parse_intake_document,
)
from bidlens.services.opportunity_intake.storage import (
    LocalSourceMaterialStorage,
    SourceMaterialStorage,
    SourceMaterialStorageError,
)
from bidlens.services.opportunity_knowledge_brief import (
    ExtractionStatus,
    SourceMaterialExtractionScopeError,
    get_or_create_extraction,
)


def readable_pdf(text="RFP response deadline September 1 2026", *, pages=1):
    output = io.BytesIO()
    writer = PdfWriter()
    for index in range(pages):
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        font_ref = writer._add_object(font)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})
        })
        stream = DecodedStreamObject()
        page_text = f"{text} page {index + 1}".replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({page_text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(output)
    return output.getvalue()


def scanned_pdf():
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(output)
    return output.getvalue()


def docx_bytes(text="Department of Health requests evaluation services"):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
            + text + "</w:t></w:r></w:p></w:body></w:document>",
        )
    return output.getvalue()


class FlakyStorage(SourceMaterialStorage):
    def __init__(self, content):
        self.content = content
        self.fail = True

    def put(self, key, content):
        self.content = content

    def get(self, key):
        if self.fail:
            raise SourceMaterialStorageError("temporary provider failure")
        return self.content

    def delete(self, key):
        pass

    def exists(self, key):
        if self.fail:
            raise SourceMaterialStorageError("temporary provider failure")
        return True


class GutsExtractionCacheTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = LocalSourceMaterialStorage(Path(self.tmp.name))
        self.org = Organization(name="Extraction", slug="guts-extraction")
        self.other_org = Organization(name="Other", slug="guts-extraction-other")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.workspace = Workspace(organization_id=self.org.id, name="Extraction", slug="guts-extraction")
        self.other_workspace = Workspace(organization_id=self.other_org.id, name="Other", slug="guts-extraction-other")
        self.db.add_all([self.workspace, self.other_workspace])
        self.db.flush()
        self.opportunity = Opportunity(
            organization_id=self.org.id, source="test", source_record_id="EXTRACT-1",
            title="Extraction", agency="Agency", opportunity_type="RFP",
            posted_date=date(2026, 7, 31), response_deadline=date(2026, 9, 1),
            qualification_status="qualified",
        )
        self.other_opportunity = Opportunity(
            organization_id=self.org.id, source="test", source_record_id="EXTRACT-2",
            title="Other", agency="Agency", opportunity_type="RFP",
            posted_date=date(2026, 7, 31), response_deadline=date(2026, 9, 2),
            qualification_status="qualified",
        )
        self.db.add_all([self.opportunity, self.other_opportunity])
        self.db.flush()
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.tmp.cleanup()

    def _material(self, content, *, filename="source.pdf", mime="application/pdf", key="files/source"):
        self.storage.put(key, content)
        material = OpportunitySourceMaterial(
            organization_id=self.org.id,
            workspace_id=self.workspace.id,
            opportunity_id=self.opportunity.id,
            material_type="rfp_document",
            original_filename=filename,
            mime_type=mime,
            byte_size=len(content),
            sha256_digest=hashlib.sha256(content).hexdigest(),
            storage_key=key,
            parse_status="COMPLETE",
        )
        self.db.add(material)
        self.db.commit()
        return material

    def _extract(self, material, **kwargs):
        return get_or_create_extraction(
            self.db,
            source_material=material,
            organization_id=self.org.id,
            workspace_id=self.workspace.id,
            opportunity_id=self.opportunity.id,
            storage=self.storage,
            **kwargs,
        )

    def test_cache_miss_parses_persists_and_hit_does_not_reparse(self):
        material = self._material(readable_pdf())
        parser = Mock(wraps=parse_intake_document)
        first = self._extract(material, parse_document=parser)
        second = self._extract(material, parse_document=parser)
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.status, ExtractionStatus.SUCCEEDED)
        self.assertIn("response deadline", first.extracted_text)
        self.assertEqual(parser.call_count, 1)
        self.assertEqual(material.parse_status, "COMPLETE")

    def test_document_parser_runs_without_an_open_database_transaction(self):
        material = self._material(readable_pdf())

        def parser(**kwargs):
            self.assertFalse(self.db.in_transaction())
            return parse_intake_document(**kwargs)

        result = self._extract(material, parse_document=parser)
        self.assertEqual(result.status, ExtractionStatus.SUCCEEDED)

    def test_changed_content_or_parser_version_creates_new_cache_row(self):
        material = self._material(readable_pdf("First"))
        first = self._extract(material)
        new_content = readable_pdf("Second")
        self.storage.put("files/changed", new_content)
        material.storage_key = "files/changed"
        material.sha256_digest = hashlib.sha256(new_content).hexdigest()
        material.byte_size = len(new_content)
        self.db.commit()
        second = self._extract(material)
        third = self._extract(material, parser_version="2")
        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(second.id, third.id)
        self.assertEqual(self.db.query(OpportunitySourceMaterialExtraction).count(), 3)

    def test_deterministic_failure_is_reused_without_reparsing(self):
        material = self._material(b"PKbad", filename="bad.docx", mime="application/octet-stream")
        parser = Mock(side_effect=IntakeDocumentError("invalid_docx", "Invalid DOCX."))
        first = self._extract(material, parse_document=parser)
        second = self._extract(material, parse_document=parser)
        self.assertEqual(first.id, second.id)
        self.assertEqual(parser.call_count, 1)
        self.assertTrue(first.warnings_json["deterministic"])

    def test_transient_storage_failure_retries_same_row_after_cooldown(self):
        content = readable_pdf()
        material = self._material(content)
        flaky = FlakyStorage(content)
        now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
        failed = get_or_create_extraction(
            self.db, source_material=material, organization_id=self.org.id,
            workspace_id=self.workspace.id, opportunity_id=self.opportunity.id,
            storage=flaky, now=now,
        )
        self.assertEqual(failed.status, ExtractionStatus.FAILED)
        flaky.fail = False
        succeeded = get_or_create_extraction(
            self.db, source_material=material, organization_id=self.org.id,
            workspace_id=self.workspace.id, opportunity_id=self.opportunity.id,
            storage=flaky, now=now + timedelta(seconds=61),
        )
        self.assertEqual(succeeded.id, failed.id)
        self.assertEqual(succeeded.status, ExtractionStatus.SUCCEEDED)

    def test_unsupported_missing_corrupt_and_scanned_files_fail_safely(self):
        cases = [
            (b"plain", "source.txt", "text/plain", "unsupported_type"),
            (b"not-pdf", "source.pdf", "application/pdf", "invalid_pdf"),
            (scanned_pdf(), "scan.pdf", "application/pdf", "no_extractable_text"),
        ]
        for index, (content, filename, mime, code) in enumerate(cases):
            material = self._material(content, filename=filename, mime=mime, key=f"files/case-{index}")
            result = self._extract(material)
            self.assertEqual(result.status, ExtractionStatus.FAILED)
            self.assertEqual(result.warnings_json["failure_code"], code)
        missing = self._material(readable_pdf(), key="files/to-delete")
        self.storage.delete(missing.storage_key)
        result = self._extract(missing)
        self.assertEqual(result.failure_category, "source_retrieval_failed")
        self.assertFalse(result.warnings_json["deterministic"])

    def test_docx_limits_and_warnings_are_persisted_without_logging_text(self):
        secret = "CONFIDENTIAL-DO-NOT-LOG-12345"
        material = self._material(
            docx_bytes(secret * 20),
            filename="source.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        parser_config = parse_intake_document.__globals__["config"]
        with self.assertLogs(
            "bidlens.services.opportunity_knowledge_brief.extraction_cache", level=logging.INFO
        ) as logs, patch.object(parser_config, "INTAKE_DOCUMENT_MAX_TEXT_CHARS", 40):
            result = self._extract(material)
        self.assertEqual(result.status, ExtractionStatus.SUCCEEDED)
        self.assertLessEqual(result.character_count, 40)
        self.assertTrue(result.warnings_json["warnings"])
        self.assertNotIn(secret, "\n".join(logs.output))

    def test_pdf_page_limit_and_warning_metadata_are_persisted(self):
        material = self._material(readable_pdf("Long source " * 20, pages=3))
        parser_config = parse_intake_document.__globals__["config"]
        with patch.object(parser_config, "INTAKE_DOCUMENT_MAX_PDF_PAGES", 1), patch.object(
            parser_config, "INTAKE_DOCUMENT_MAX_TEXT_CHARS", 50
        ):
            result = self._extract(material)
        self.assertEqual(result.page_count, 1)
        self.assertTrue(result.warnings_json["metadata"]["capped_by_chars"])

    def test_scope_mismatch_and_cross_opportunity_are_rejected(self):
        material = self._material(readable_pdf())
        with self.assertRaises(SourceMaterialExtractionScopeError):
            get_or_create_extraction(
                self.db, source_material=material, organization_id=self.other_org.id,
                workspace_id=self.workspace.id, opportunity_id=self.opportunity.id, storage=self.storage,
            )
        with self.assertRaises(SourceMaterialExtractionScopeError):
            get_or_create_extraction(
                self.db, source_material=material, organization_id=self.org.id,
                workspace_id=self.workspace.id, opportunity_id=self.other_opportunity.id, storage=self.storage,
            )


if __name__ == "__main__":
    unittest.main()
