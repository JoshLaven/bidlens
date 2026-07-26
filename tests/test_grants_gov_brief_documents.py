import datetime as dt
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bidlens.services.grants_gov_documents import (
    grants_gov_attachment_download_url,
    grants_gov_document_metadata,
    grants_gov_document_resources,
)
from bidlens.services.research.brief_generator import build_brief_request_payload
from bidlens.services.research.document_fetcher import (
    fetch_opportunity_attachment_metadata,
    fetch_opportunity_documents,
)


def _opportunity(*, source="grants_gov", raw_source_payload=None, sam_notice_id=None):
    return SimpleNamespace(
        id=42,
        bidlens_id=None,
        source=source,
        source_record_id="grant-42",
        external_source_key=f"{source}:grant-42",
        solicitation_number="TEST-42",
        sam_notice_id=sam_notice_id,
        title="Public health grant",
        agency="Department of Health and Human Services",
        opportunity_type="Grant" if source == "grants_gov" else "Solicitation",
        posted_date=dt.date(2026, 7, 1),
        response_deadline=dt.date(2026, 8, 1),
        naics=None,
        naics_title=None,
        set_aside=None,
        source_url="https://www.grants.gov/search-results-detail/grant-42",
        sam_url=None,
        description="Grant synopsis used as supplemental context.",
        description_text="Grant synopsis used as supplemental context.",
        description_url=None,
        raw_source_payload=raw_source_payload or {},
    )


class GrantsGovBriefDocumentTests(unittest.TestCase):
    def test_discovers_folder_attachments_and_constructs_download_urls(self):
        opp = _opportunity(raw_source_payload={
            "synopsisAttachmentFolders": [{
                "folderName": "Application package",
                "synopsisAttachments": [
                    {"id": 101, "fileName": "NOFO.pdf", "mimeType": "application/pdf", "fileLobSize": 1200},
                    {"id": 102, "fileName": "Instructions.docx", "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
                ],
            }]
        })

        metadata = grants_gov_document_metadata(opp)
        resources = grants_gov_document_resources(opp)

        self.assertEqual(len(metadata["folders"][0]["attachments"]), 2)
        self.assertEqual(resources[0]["source_url"], grants_gov_attachment_download_url(101))
        self.assertEqual([item["filename"] for item in resources], ["NOFO.pdf", "Instructions.docx"])

    def test_discovers_nested_document_urls_and_deduplicates(self):
        url = "https://files.example.test/notice.txt"
        opp = _opportunity(raw_source_payload={
            "detail_payload": {"data": {"synopsisDocumentURLs": [url, {"fileName": "Notice", "url": url}, {"description": "Budget document", "docUrl": "https://files.example.test/budget.pdf"}]}}
        })

        resources = grants_gov_document_resources(opp)

        self.assertEqual(len(resources), 2)
        self.assertEqual(resources[0]["filename"], url)
        self.assertEqual(resources[1]["filename"], "Budget document")
        attachments = fetch_opportunity_attachment_metadata(opp)
        self.assertEqual([item["file_kind"] for item in attachments["attachments"]], ["txt", "pdf"])

    @patch("bidlens.services.research.document_fetcher.extract_pdf_text")
    @patch("bidlens.services.research.document_fetcher._download_attachment", return_value=b"pdf bytes")
    def test_supported_document_is_extracted_into_normalized_result(self, _download, extract_pdf):
        extract_pdf.return_value = {"extracted_text": "Required project narrative.", "pages_extracted": 2, "total_characters": 27}
        opp = _opportunity(raw_source_payload={"synopsisAttachmentFolders": [{"synopsisAttachments": [{"id": 201, "fileName": "NOFO.pdf", "mimeType": "application/pdf"}]}]})

        result = fetch_opportunity_documents(opp)

        self.assertEqual(result["documents"][0]["extracted_text"], "Required project narrative.")
        self.assertEqual(result["summary"]["pdfs_processed"], 1)
        self.assertEqual(result["summary"]["pages_extracted"], 2)
        self.assertEqual(result["summary"]["discovery_method"], "grants_gov_raw_source_payload")

    @patch("bidlens.services.research.document_fetcher.extract_txt_text")
    @patch("bidlens.services.research.document_fetcher._download_attachment")
    def test_multiple_failed_and_unsupported_attachments_degrade_gracefully(self, download, extract_txt):
        download.side_effect = lambda url: None if "301" in url else b"text bytes"
        extract_txt.return_value = {"extracted_text": "Readable instructions.", "pages_extracted": 1, "total_characters": 22}
        opp = _opportunity(raw_source_payload={"synopsisAttachmentFolders": [{"synopsisAttachments": [
            {"id": 301, "fileName": "broken.pdf", "mimeType": "application/pdf"},
            {"id": 302, "fileName": "instructions.txt", "mimeType": "text/plain"},
            {"id": 303, "fileName": "budget.xlsx", "mimeType": "application/vnd.ms-excel"},
        ]}]})

        result = fetch_opportunity_documents(opp)

        self.assertEqual([doc["filename"] for doc in result["documents"]], ["instructions.txt"])
        self.assertEqual(result["summary"]["extraction_failures"], 1)
        self.assertEqual(result["summary"]["spreadsheets_skipped"], 1)

    @patch("bidlens.services.research.document_fetcher.extract_docx_text")
    @patch("bidlens.services.research.document_fetcher._download_attachment", return_value=b"docx bytes")
    def test_brief_payload_uses_grants_documents_before_description(self, _download, extract_docx):
        extract_docx.return_value = {"extracted_text": "Applicants must submit a work plan.", "pages_extracted": 1, "total_characters": 35}
        opp = _opportunity(raw_source_payload={"synopsisAttachmentFolders": [{"synopsisAttachments": [{"id": 401, "fileName": "Application.docx", "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}]}]})

        payload = build_brief_request_payload(opp)

        self.assertEqual(payload["source_basis"], "solicitation_documents")
        self.assertTrue(payload["used_solicitation_documents"])
        self.assertEqual(payload["filenames_processed"], ["Application.docx"])
        self.assertEqual(payload["source_summary"]["docs_processed"], 1)
        self.assertLess(payload["text_for_brief"].index("Applicants must submit"), payload["text_for_brief"].index("Grant synopsis"))
        self.assertIn("Supplemental Grants.gov Description", payload["text_for_brief"])
        self.assertIn("Attachments found for Grants.gov: 1", payload["text_for_brief"])
        self.assertNotIn("Supplemental SAM Description", payload["text_for_brief"])

    def test_description_only_fallback_has_correct_flags(self):
        payload = build_brief_request_payload(_opportunity())

        self.assertEqual(payload["source_basis"], "description_only")
        self.assertFalse(payload["used_solicitation_documents"])
        self.assertEqual(payload["filenames_processed"], [])
        self.assertIn("Grant synopsis used as supplemental context.", payload["text_for_brief"])

    @patch("bidlens.services.research.document_fetcher._fetch_public_file_resources")
    def test_sam_discovery_path_is_preserved(self, fetch_sam):
        fetch_sam.return_value = ([], {"total_attachments_found": 0})
        opp = _opportunity(source="sam", sam_notice_id="sam-notice")
        opp.sam_url = None

        result = fetch_opportunity_attachment_metadata(opp)

        fetch_sam.assert_called_once_with(opp)
        self.assertEqual(result["attachments"], [])

    @patch("bidlens.services.research.document_fetcher.requests.get")
    def test_download_http_failure_is_non_fatal(self, get):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.raise_for_status.side_effect = Exception("not used")
        # requests exceptions are handled by the downloader; use a real request error.
        import requests
        response.raise_for_status.side_effect = requests.HTTPError("403")
        get.return_value = response
        opp = _opportunity(raw_source_payload={"synopsisAttachmentFolders": [{"synopsisAttachments": [{"id": 501, "fileName": "restricted.pdf", "mimeType": "application/pdf"}]}]})

        result = fetch_opportunity_documents(opp)

        self.assertEqual(result["documents"], [])
        self.assertEqual(result["summary"]["extraction_failures"], 1)


if __name__ == "__main__":
    unittest.main()
