import io
import json
import logging
import tempfile
import unittest
import zipfile
from email.message import EmailMessage
from email.policy import default
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import Opportunity, OpportunitySourceMaterial, Organization, OrganizationMembership, User, Workspace
from bidlens.services.opportunity_intake import (
    IntakeEmailError,
    IntakeEmailExtractionResult,
    IntakeExtractionError,
    LocalSourceMaterialStorage,
    OpenAIIntakeEmailExtractor,
    OpportunityDuplicateError,
    OpportunityPublisher,
    assemble_email_extraction_input,
    parse_email_extraction_payload,
    parse_intake_email,
    process_email_file,
    validate_intake_email,
)


def docx_bytes(text="Attachment RFP deadline November 15 2026"):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
            + text
            + "</w:t></w:r></w:p></w:body></w:document>",
        )
    return output.getvalue()


def pdf_bytes(text="Attachment RFP response deadline November 15 2026"):
    output = io.BytesIO()
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})})
    stream = DecodedStreamObject()
    safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream.set_data(f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(output)
    return output.getvalue()


def email_bytes(
    *,
    subject="Community Evaluation RFP",
    body="Please review this opportunity. Responses are due November 15, 2026.",
    html=None,
    message_id="<phase6@example.test>",
    attachments=(),
):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = "Joséphine Example <sender@example.test>"
    message["To"] = "reviewer@example.test"
    message["Cc"] = "team@example.test"
    message["Date"] = "Tue, 28 Jul 2026 12:30:00 -0700"
    if message_id is not None:
        message["Message-ID"] = message_id
    if html is not None:
        message.set_content("This client does not display HTML.")
        message.add_alternative(html, subtype="html")
    else:
        message.set_content(body)
    for filename, mime, content in attachments:
        maintype, subtype = mime.split("/", 1)
        message.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)
    return message.as_bytes(policy=default)


def extraction_payload(**values):
    defaults = {
        "title": "Community Evaluation RFP",
        "client": "Department of Health",
        "response_deadline": "2026-11-15",
        "solicitation_number": "EMAIL-RFP-42",
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
                "source": "attachment" if field != "title" else "email",
            }
            for field, value in defaults.items()
        },
        "warnings": [],
    }


class FakeEmailExtractor:
    def __init__(self, *, error=None, payload=None):
        self.error = error
        self.payload = payload or extraction_payload()
        self.calls = 0
        self.inputs = []

    def extract(self, text):
        self.calls += 1
        self.inputs.append(text)
        if self.error:
            raise self.error
        return parse_email_extraction_payload(self.payload, model="fake-model")


class EmailParsingTests(unittest.TestCase):
    def test_plain_text_metadata_encoded_headers_and_missing_message_id(self):
        raw = email_bytes(subject="Café Opportunity", message_id=None)
        parsed = parse_intake_email(filename="mail.eml", mime_type="message/rfc822", content=raw)
        self.assertEqual(parsed.subject, "Café Opportunity")
        self.assertEqual(parsed.sender, "sender@example.test")
        self.assertEqual(parsed.to_recipients, ("reviewer@example.test",))
        self.assertIsNone(parsed.internet_message_id)
        self.assertEqual(parsed.body_source, "plain")
        self.assertIn("Responses are due", parsed.body_text)

    def test_html_only_is_text_only_and_does_not_expose_active_content(self):
        raw = b"From: sender@example.test\r\nTo: reviewer@example.test\r\nSubject: HTML RFP\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>Visible request</p><script>SECRET_SCRIPT()</script><style>.x{}</style><img src='https://remote.invalid/pixel'>"
        parsed = parse_intake_email(filename="mail.eml", mime_type="message/rfc822", content=raw)
        self.assertEqual(parsed.body_source, "html")
        self.assertIn("Visible request", parsed.body_text)
        self.assertNotIn("SECRET_SCRIPT", parsed.body_text)
        self.assertNotIn("remote.invalid", parsed.body_text)

    def test_multipart_alternative_prefers_plain_and_strips_quoted_reply(self):
        message = EmailMessage()
        message["Subject"] = "Reply"
        message.set_content("Current request\n\nOn Mon, Person wrote:\n> old secret history")
        message.add_alternative("<p>HTML alternative</p>", subtype="html")
        parsed = parse_intake_email(filename="reply.eml", mime_type="message/rfc822", content=message.as_bytes())
        self.assertEqual(parsed.body_text, "Current request")
        self.assertNotIn("HTML alternative", parsed.body_text)
        self.assertNotIn("old secret", parsed.body_text)

    def test_forwarded_inline_content_and_malformed_optional_date_remain_usable(self):
        raw = b"Subject: Forwarded RFP\r\nDate: definitely-not-a-date\r\n\r\nForwarded message\nClient needs evaluation services."
        parsed = parse_intake_email(filename="forward.eml", mime_type="text/plain", content=raw)
        self.assertIn("Forwarded message", parsed.body_text)
        self.assertIsNone(parsed.sent_at)
        self.assertTrue(any("sent date" in warning for warning in parsed.warnings))

    def test_nested_mime_does_not_recursively_extract_attached_email(self):
        outer = EmailMessage()
        outer["Subject"] = "Outer"
        outer.set_content("Outer body")
        nested = EmailMessage()
        nested["Subject"] = "Nested"
        nested.set_content("NESTED SECRET BODY")
        outer.add_attachment(nested, filename="forwarded.eml")
        parsed = parse_intake_email(filename="outer.eml", mime_type="message/rfc822", content=outer.as_bytes())
        self.assertIn("Outer body", parsed.body_text)
        self.assertNotIn("NESTED SECRET", parsed.body_text)
        self.assertTrue(any(item["reason"] == "unsupported_type" for item in parsed.skipped_attachments))

    def test_unsupported_msg_and_invalid_input_feedback(self):
        with self.assertRaises(IntakeEmailError) as raised:
            validate_intake_email(filename="outlook.msg", mime_type="application/vnd.ms-outlook", content=b"msg")
        self.assertEqual(raised.exception.code, "unsupported_msg")
        with self.assertRaises(IntakeEmailError):
            validate_intake_email(filename="mail.eml", mime_type="image/png", content=b"mail")
        fallback = parse_intake_email(filename="mail.eml", mime_type="text/plain", content=b"partially readable malformed input")
        self.assertIn("partially readable", fallback.body_text)

    def test_attachment_count_individual_and_total_limits(self):
        attachments = [(f"file-{index}.pdf", "application/pdf", pdf_bytes()) for index in range(3)]
        raw = email_bytes(attachments=attachments)
        with patch("bidlens.services.opportunity_intake.email_parsing.config.INTAKE_EMAIL_MAX_ATTACHMENTS", 1):
            parsed = parse_intake_email(filename="mail.eml", mime_type="message/rfc822", content=raw)
            self.assertEqual(len(parsed.attachments), 1)
            self.assertEqual(len(parsed.skipped_attachments), 2)
        with patch("bidlens.services.opportunity_intake.email_parsing.config.INTAKE_EMAIL_MAX_ATTACHMENT_BYTES", 2):
            parsed = parse_intake_email(filename="mail.eml", mime_type="message/rfc822", content=raw)
            self.assertFalse(parsed.attachments)
        with patch("bidlens.services.opportunity_intake.email_parsing.config.INTAKE_EMAIL_MAX_TOTAL_ATTACHMENT_BYTES", len(pdf_bytes()) + 1):
            parsed = parse_intake_email(filename="mail.eml", mime_type="message/rfc822", content=raw)
            self.assertEqual(len(parsed.attachments), 1)


class EmailIntakeServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = LocalSourceMaterialStorage(Path(self.tmp.name))
        self.org = Organization(name="Email Intake", slug="email-intake")
        self.db.add(self.org)
        self.db.flush()
        self.workspace = Workspace(organization_id=self.org.id, name="Email", slug="email")
        self.user = User(email="email@example.test", organization_id=self.org.id)
        self.db.add_all([self.workspace, self.user])
        self.db.flush()
        self.db.add(OrganizationMembership(organization_id=self.org.id, user_id=self.user.id, role="member"))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.tmp.cleanup()

    def _process(self, raw, *, filename="message.eml", mime="message/rfc822", extractor=None):
        return process_email_file(
            self.db,
            self.storage,
            organization_id=self.org.id,
            workspace_id=self.workspace.id,
            user_id=self.user.id,
            filename=filename,
            mime_type=mime,
            content=raw,
            extractor=extractor or FakeEmailExtractor(),
        )

    def test_original_and_pdf_docx_children_are_stored_parsed_and_bounded_once(self):
        raw = email_bytes(attachments=(
            ("../brief.pdf", "application/pdf", pdf_bytes()),
            ("solicitação.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", docx_bytes()),
            ("notes.txt", "text/plain", b"unsupported"),
        ))
        extractor = FakeEmailExtractor()
        result = self._process(raw, filename="../../source.eml", extractor=extractor)
        self.assertEqual(extractor.calls, 1)
        self.assertIn("EMAIL SUBJECT", extractor.inputs[0])
        self.assertIn("EMAIL BODY", extractor.inputs[0])
        self.assertIn("PRIMARY AUTHORITY", extractor.inputs[0])
        self.assertEqual(result.email_material.original_filename, "source.eml")
        self.assertEqual(result.email_material.material_type, "email")
        self.assertEqual(result.email_material.internet_message_id, "<phase6@example.test>")
        self.assertEqual(len(result.attachment_materials), 2)
        for material in result.attachment_materials:
            self.assertEqual(material.parent_material_id, result.email_material.id)
            self.assertEqual(material.parse_status, "COMPLETE")
            self.assertTrue(self.storage.exists(material.storage_key))
            self.assertEqual(len(material.sha256_digest), 64)
        self.assertTrue(any("skipped" in warning for warning in result.warnings))
        self.assertEqual(result.draft.extraction_metadata_json["openai_request_count"], 1)
        self.assertEqual(result.draft.extraction_metadata_json["source_attribution"]["client"], "attachment")
        self.assertEqual(self.db.query(Opportunity).count(), 0)

    def test_invalid_supported_attachments_warn_without_aborting_email(self):
        raw = email_bytes(attachments=(
            ("bad.pdf", "application/pdf", b"not-pdf"),
            ("bad.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"PKbad"),
        ))
        result = self._process(raw)
        self.assertEqual(len(result.attachment_materials), 1)
        self.assertEqual(result.attachment_materials[0].original_filename, "bad.docx")
        self.assertEqual(result.attachment_materials[0].parse_status, "FAILED")
        self.assertTrue(any("bad.pdf" in warning for warning in result.warnings))
        self.assertTrue(any("bad.docx" in warning for warning in result.warnings))
        self.assertEqual(result.draft.status, "READY")

    def test_timeout_retains_subject_source_and_usable_review(self):
        extractor = FakeEmailExtractor(error=IntakeExtractionError("timeout", "Extraction timed out."))
        result = self._process(email_bytes(subject="Deterministic Subject"), extractor=extractor)
        self.assertEqual(extractor.calls, 1)
        self.assertEqual(result.draft.candidate_fields_json["title"], "Deterministic Subject")
        self.assertEqual(result.draft.status, "READY")
        self.assertTrue(self.storage.exists(result.email_material.storage_key))
        self.assertIn("Extraction timed out.", result.warnings)

    def test_input_limit_and_missing_or_ambiguous_values(self):
        parsed = parse_intake_email(filename="mail.eml", mime_type="message/rfc822", content=email_bytes(body="x" * 1000))
        assembled = assemble_email_extraction_input(parsed, [("large.pdf", "y" * 1000)], max_chars=200)
        self.assertEqual(len(assembled), 200)
        result = parse_email_extraction_payload(
            extraction_payload(client=None, response_deadline="question deadline tomorrow"), model="fake"
        )
        self.assertIsNone(result.result.candidate.client)
        self.assertIsNone(result.result.candidate.response_deadline)
        self.assertTrue(any("not a valid date" in warning for warning in result.result.warnings))

    def test_openai_email_extractor_makes_one_strict_request(self):
        class Response:
            output_text = json.dumps(extraction_payload())
            usage = None
        class Responses:
            def __init__(self): self.calls = []
            def create(self, **kwargs): self.calls.append(kwargs); return Response()
        responses = Responses()
        fake_client = type("Client", (), {"responses": responses})()
        with patch("openai.OpenAI", return_value=fake_client), patch(
            "bidlens.services.opportunity_intake.email_extraction.config.OPENAI_API_KEY", "key"
        ):
            result = OpenAIIntakeEmailExtractor().extract("PRIVATE SOURCE")
        self.assertEqual(len(responses.calls), 1)
        self.assertTrue(responses.calls[0]["text"]["format"]["strict"])
        self.assertIn("primary authority", responses.calls[0]["instructions"])
        self.assertEqual(result.source_attribution["client"], "attachment")

    def test_source_content_is_not_logged_on_failure(self):
        class FailingResponses:
            def create(self, **kwargs): raise TimeoutError("timeout")
        fake_client = type("Client", (), {"responses": FailingResponses()})()
        with self.assertLogs("bidlens.services.opportunity_intake.email_extraction", logging.WARNING) as captured, patch(
            "openai.OpenAI", return_value=fake_client
        ), patch("bidlens.services.opportunity_intake.email_extraction.config.OPENAI_API_KEY", "key"):
            with self.assertRaises(IntakeExtractionError):
                OpenAIIntakeEmailExtractor().extract("HIGHLY PRIVATE EMAIL BODY")
        self.assertNotIn("HIGHLY PRIVATE", " ".join(captured.output))

    def test_message_hash_message_id_and_attachment_hash_block_retry(self):
        first = self._process(email_bytes())
        published = OpportunityPublisher.publish_reviewed_draft(
            self.db,
            draft_id=first.draft.id,
            publishing_user=self.user,
            reviewed_candidate=first.draft.candidate_fields_json,
            add_to_shortlist=False,
            idempotency_key=first.draft.publish_idempotency_key,
        )
        self.assertIsNotNone(published.opportunity_id)
        variants = [
            email_bytes(),
            email_bytes(body="Different body", message_id="<phase6@example.test>"),
        ]
        for raw in variants:
            retry = self._process(raw)
            with self.assertRaises(OpportunityDuplicateError):
                OpportunityPublisher.publish_reviewed_draft(
                    self.db,
                    draft_id=retry.draft.id,
                    publishing_user=self.user,
                    reviewed_candidate=retry.draft.candidate_fields_json,
                    add_to_shortlist=False,
                    idempotency_key=retry.draft.publish_idempotency_key,
                )
            self.db.rollback()

    def test_duplicate_attachment_hash_blocks_different_email(self):
        attachment = pdf_bytes("Unique attachment solicitation")
        first = self._process(email_bytes(
            message_id="<attachment-one@example.test>",
            attachments=(("one.pdf", "application/pdf", attachment),),
        ))
        OpportunityPublisher.publish_reviewed_draft(
            self.db,
            draft_id=first.draft.id,
            publishing_user=self.user,
            reviewed_candidate=first.draft.candidate_fields_json,
            add_to_shortlist=False,
            idempotency_key=first.draft.publish_idempotency_key,
        )
        retry = self._process(email_bytes(
            subject="Different email title",
            body="Different body",
            message_id="<attachment-two@example.test>",
            attachments=(("renamed.pdf", "application/pdf", attachment),),
        ))
        with self.assertRaises(OpportunityDuplicateError):
            OpportunityPublisher.publish_reviewed_draft(
                self.db,
                draft_id=retry.draft.id,
                publishing_user=self.user,
                reviewed_candidate=retry.draft.candidate_fields_json,
                add_to_shortlist=False,
                idempotency_key=retry.draft.publish_idempotency_key,
            )

    def test_same_message_id_in_another_organization_is_allowed(self):
        first = self._process(email_bytes())
        OpportunityPublisher.publish_reviewed_draft(
            self.db,
            draft_id=first.draft.id,
            publishing_user=self.user,
            reviewed_candidate=first.draft.candidate_fields_json,
            add_to_shortlist=False,
            idempotency_key=first.draft.publish_idempotency_key,
        )
        other_org = Organization(name="Other Email Org", slug="other-email-org")
        self.db.add(other_org)
        self.db.flush()
        other_workspace = Workspace(organization_id=other_org.id, name="Other", slug="other")
        other_user = User(email="other-email@example.test", organization_id=other_org.id)
        self.db.add_all([other_workspace, other_user])
        self.db.flush()
        self.db.add(OrganizationMembership(organization_id=other_org.id, user_id=other_user.id, role="member"))
        self.db.commit()
        other = process_email_file(
            self.db,
            self.storage,
            organization_id=other_org.id,
            workspace_id=other_workspace.id,
            user_id=other_user.id,
            filename="same-id.eml",
            mime_type="message/rfc822",
            content=email_bytes(body="Other organization copy"),
            extractor=FakeEmailExtractor(payload=extraction_payload(
                title="Other Opportunity", solicitation_number="OTHER-RFP"
            )),
        )
        published = OpportunityPublisher.publish_reviewed_draft(
            self.db,
            draft_id=other.draft.id,
            publishing_user=other_user,
            reviewed_candidate=other.draft.candidate_fields_json,
            add_to_shortlist=False,
            idempotency_key=other.draft.publish_idempotency_key,
        )
        self.assertIsNotNone(published.opportunity_id)

    def test_reviewed_values_win_and_all_materials_remain_associated(self):
        upload = self._process(email_bytes(attachments=(("rfp.pdf", "application/pdf", pdf_bytes()),)))
        published = OpportunityPublisher.publish_reviewed_draft(
            self.db,
            draft_id=upload.draft.id,
            publishing_user=self.user,
            reviewed_candidate={
                "title": "Corrected by user",
                "client": "Corrected client",
                "response_deadline": "2026-12-01",
                "solicitation_number": "CORRECTED-EMAIL",
                "opportunity_type": "RFP",
                "description": "Corrected description",
            },
            add_to_shortlist=False,
            idempotency_key=upload.draft.publish_idempotency_key,
        )
        opportunity = self.db.get(Opportunity, published.opportunity_id)
        self.assertEqual(opportunity.title, "Corrected by user")
        self.assertEqual(opportunity.agency, "Corrected client")
        materials = self.db.query(OpportunitySourceMaterial).filter_by(intake_draft_id=upload.draft.id).all()
        self.assertEqual(len(materials), 2)
        self.assertTrue(all(item.opportunity_id == opportunity.id for item in materials))


if __name__ == "__main__":
    unittest.main()
