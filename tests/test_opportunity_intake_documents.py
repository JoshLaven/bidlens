import io
import json
import logging
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import (
    Opportunity,
    OpportunitySourceMaterial,
    Organization,
    OrganizationMembership,
    User,
    Workspace,
)
from bidlens.services.opportunity_intake import (
    IntakeCandidate,
    IntakeDocumentError,
    IntakeExtractionError,
    IntakeExtractionResult,
    LocalSourceMaterialStorage,
    OpenAIIntakeDocumentExtractor,
    OpportunityDuplicateError,
    OpportunityPublisher,
    parse_extraction_payload,
    parse_intake_document,
    process_rfp_document,
    validate_intake_document,
)


def readable_pdf(text="RFP 42 Response deadline October 30 2026"):
    output = io.BytesIO()
    writer = PdfWriter()
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
    safe_text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream.set_data(f"BT /F1 12 Tf 72 720 Td ({safe_text}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(output)
    return output.getvalue()


def docx_bytes(text="Department of Health requests evaluation services"):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"></Types>',
        )
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
            + text
            + "</w:t></w:r></w:p></w:body></w:document>",
        )
    return output.getvalue()


def extraction_payload(**values):
    defaults = {
        "title": "Community Health Evaluation",
        "client": "Department of Health",
        "response_deadline": "2026-10-30",
        "solicitation_number": "RFP-42",
        "opportunity_type": "RFP",
        "description": "Evaluation services solicitation.",
    }
    defaults.update(values)
    return {
        **{
            field: {
                "value": value,
                "confidence": "high" if value else "unknown",
                "evidence": f"Evidence for {field}" if value else None,
            }
            for field, value in defaults.items()
        },
        "warnings": [],
    }


class FakeExtractor:
    def __init__(self, *, error=None, payload=None):
        self.error = error
        self.payload = payload or extraction_payload()
        self.calls = 0

    def extract(self, text):
        self.calls += 1
        if self.error:
            raise self.error
        return parse_extraction_payload(self.payload, model="fake-model")


class OpportunityIntakeDocumentTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = LocalSourceMaterialStorage(Path(self.tmp.name))
        self.org = Organization(name="Document Intake", slug="document-intake")
        self.db.add(self.org)
        self.db.flush()
        self.workspace = Workspace(organization_id=self.org.id, name="Documents", slug="documents")
        self.user = User(email="documents@example.com", organization_id=self.org.id)
        self.db.add_all([self.workspace, self.user])
        self.db.flush()
        self.db.add(OrganizationMembership(
            organization_id=self.org.id, user_id=self.user.id, role="member"
        ))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.tmp.cleanup()

    def _process(self, content, *, filename="rfp.pdf", mime="application/pdf", extractor=None):
        return process_rfp_document(
            self.db,
            self.storage,
            organization_id=self.org.id,
            workspace_id=self.workspace.id,
            user_id=self.user.id,
            filename=filename,
            mime_type=mime,
            content=content,
            extractor=extractor or FakeExtractor(),
        )

    def test_readable_pdf_and_docx_parsing(self):
        pdf = parse_intake_document(
            filename="rfp.pdf", mime_type="application/pdf", content=readable_pdf()
        )
        docx = parse_intake_document(
            filename="rfp.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            content=docx_bytes(),
        )
        self.assertIn("Response deadline", pdf.extracted_text)
        self.assertEqual(pdf.parser_type, "pypdf")
        self.assertIn("Department of Health", docx.extracted_text)
        self.assertEqual(docx.parser_type, "docx_xml")

    def test_upload_stores_original_safe_key_hash_and_candidate_metadata(self):
        content = docx_bytes()
        extractor = FakeExtractor()
        result = self._process(
            content,
            filename="../../unsafe rfp.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            extractor=extractor,
        )
        self.assertEqual(extractor.calls, 1)
        self.assertEqual(result.draft.intake_method, "document")
        self.assertEqual(result.draft.status, "READY")
        self.assertEqual(result.draft.candidate_fields_json["title"], "Community Health Evaluation")
        self.assertEqual(result.draft.candidate_fields_json["client"], "Department of Health")
        self.assertEqual(result.draft.candidate_fields_json["response_deadline"], "2026-10-30")
        self.assertEqual(result.draft.candidate_fields_json["solicitation_number"], "RFP-42")
        self.assertEqual(result.material.original_filename, "unsafe rfp.docx")
        self.assertNotIn("unsafe", result.material.storage_key)
        self.assertEqual(self.storage.get(result.material.storage_key), content)
        self.assertEqual(len(result.material.sha256_digest), 64)
        self.assertEqual(result.draft.extraction_metadata_json["openai_request_count"], 1)
        self.assertIn("confidence", result.draft.extraction_metadata_json)
        self.assertIn("evidence", result.draft.extraction_metadata_json)
        self.assertEqual(self.db.query(Opportunity).count(), 0)

    def test_empty_oversized_unsupported_and_mismatch_are_rejected(self):
        cases = [
            ({"filename": "empty.pdf", "mime_type": "application/pdf", "content": b""}, "empty_file"),
            ({"filename": "legacy.doc", "mime_type": "application/msword", "content": b"old"}, "unsupported_legacy_doc"),
            ({"filename": "image.png", "mime_type": "image/png", "content": b"png"}, "unsupported_type"),
            ({"filename": "rfp.pdf", "mime_type": "image/png", "content": readable_pdf()}, "type_mismatch"),
            ({"filename": "rfp.pdf", "mime_type": "application/pdf", "content": b"not pdf"}, "invalid_pdf"),
        ]
        for kwargs, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(IntakeDocumentError) as raised:
                    validate_intake_document(**kwargs)
                self.assertEqual(raised.exception.code, code)
        with self.assertRaises(IntakeDocumentError) as raised:
            validate_intake_document(
                filename="large.pdf", mime_type="application/pdf", content=readable_pdf(), max_bytes=3
            )
        self.assertEqual(raised.exception.code, "file_too_large")
        self.assertEqual(self.db.query(OpportunitySourceMaterial).count(), 0)

    def test_malformed_docx_and_scanned_pdf_degrade_safely(self):
        with self.assertRaises(IntakeDocumentError):
            parse_intake_document(
                filename="bad.docx", mime_type="application/octet-stream", content=b"PKnot-a-zip"
            )
        scanned = PdfWriter()
        scanned.add_blank_page(width=612, height=792)
        output = io.BytesIO()
        scanned.write(output)
        extractor = FakeExtractor()
        result = self._process(output.getvalue(), extractor=extractor)
        self.assertEqual(extractor.calls, 0)
        self.assertEqual(result.material.parse_error_code, "no_readable_text")
        self.assertEqual(result.draft.candidate_fields_json, {})
        self.assertTrue(any("OCR" in warning for warning in result.warnings))

    def test_docx_expansion_limit_is_enforced(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", "<Types />")
            archive.writestr("word/document.xml", "<document />")
            for index in range(500):
                archive.writestr(f"word/media/{index}.txt", "x")
        with self.assertRaises(IntakeDocumentError):
            parse_intake_document(
                filename="large.docx",
                mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                content=output.getvalue(),
            )

    def test_timeout_and_invalid_model_output_leave_usable_review_draft(self):
        timeout = FakeExtractor(error=IntakeExtractionError("timeout", "Extraction timed out."))
        result = self._process(readable_pdf(), extractor=timeout)
        self.assertEqual(timeout.calls, 1)
        self.assertEqual(result.draft.status, "READY")
        self.assertEqual(result.draft.candidate_fields_json, {})
        self.assertEqual(result.material.parse_status, "COMPLETE")
        self.assertIn("Extraction timed out.", result.warnings)
        with self.assertRaises(IntakeExtractionError):
            parse_extraction_payload({"title": {}}, model="fake")

    def test_missing_and_invalid_extracted_values_remain_blank_with_warning(self):
        result = parse_extraction_payload(
            extraction_payload(
                title=None,
                response_deadline="October someday",
                solicitation_number=None,
            ),
            model="fake",
        )
        self.assertIsNone(result.candidate.title)
        self.assertIsNone(result.candidate.response_deadline)
        self.assertIsNone(result.candidate.solicitation_number)
        self.assertTrue(any("not a valid date" in warning for warning in result.warnings))

    def test_openai_extractor_makes_one_strict_request(self):
        class Response:
            output_text = json.dumps(extraction_payload())
            usage = None

        class Responses:
            def __init__(self): self.calls = []
            def create(self, **kwargs): self.calls.append(kwargs); return Response()

        responses = Responses()
        fake_client = type("Client", (), {"responses": responses})()
        with patch("openai.OpenAI", return_value=fake_client), patch(
            "bidlens.services.opportunity_intake.document_extraction.config.OPENAI_API_KEY", "test-key"
        ):
            result = OpenAIIntakeDocumentExtractor().extract("secret source text")
        self.assertEqual(len(responses.calls), 1)
        self.assertTrue(responses.calls[0]["text"]["format"]["strict"])
        self.assertEqual(result.candidate.client, "Department of Health")

    def test_source_text_is_not_logged_on_provider_failure(self):
        class FailingResponses:
            def create(self, **kwargs): raise TimeoutError("provider timeout")

        fake_client = type("Client", (), {"responses": FailingResponses()})()
        with self.assertLogs(
            "bidlens.services.opportunity_intake.document_extraction", logging.WARNING
        ) as captured, patch("openai.OpenAI", return_value=fake_client), patch(
            "bidlens.services.opportunity_intake.document_extraction.config.OPENAI_API_KEY", "test-key"
        ):
            with self.assertRaises(IntakeExtractionError):
                OpenAIIntakeDocumentExtractor().extract("HIGHLY SECRET SOURCE TEXT")
        self.assertNotIn("HIGHLY SECRET", " ".join(captured.output))

    def test_published_document_remains_associated_and_reviewed_edit_wins(self):
        upload = self._process(
            docx_bytes(),
            filename="rfp.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        published = OpportunityPublisher.publish_reviewed_draft(
            self.db,
            draft_id=upload.draft.id,
            publishing_user=self.user,
            reviewed_candidate={
                "title": "User corrected title",
                "client": "User corrected client",
                "response_deadline": "2026-11-15",
                "solicitation_number": "CORRECTED-42",
                "opportunity_type": "RFP",
                "description": "Corrected description",
            },
            add_to_shortlist=False,
            idempotency_key=upload.draft.publish_idempotency_key,
        )
        opportunity = self.db.get(Opportunity, published.opportunity_id)
        material = self.db.get(OpportunitySourceMaterial, upload.material.id)
        self.assertEqual(opportunity.title, "User corrected title")
        self.assertEqual(opportunity.agency, "User corrected client")
        self.assertEqual(material.opportunity_id, opportunity.id)
        self.assertEqual(material.intake_draft_id, upload.draft.id)

    def test_same_uploaded_document_cannot_publish_a_second_opportunity(self):
        content = docx_bytes()
        first = self._process(
            content,
            filename="first.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        OpportunityPublisher.publish_reviewed_draft(
            self.db,
            draft_id=first.draft.id,
            publishing_user=self.user,
            reviewed_candidate=first.draft.candidate_fields_json,
            add_to_shortlist=False,
            idempotency_key=first.draft.publish_idempotency_key,
        )
        second = self._process(
            content,
            filename="retry.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        with self.assertRaises(OpportunityDuplicateError):
            OpportunityPublisher.publish_reviewed_draft(
                self.db,
                draft_id=second.draft.id,
                publishing_user=self.user,
                reviewed_candidate=second.draft.candidate_fields_json,
                add_to_shortlist=False,
                idempotency_key=second.draft.publish_idempotency_key,
            )


if __name__ == "__main__":
    unittest.main()
