import logging
import os
import re
from html import unescape
from urllib.parse import urljoin, urlparse

import requests

from ...sam_client import _is_url_like
from .document_text_parser import extract_doc_text, extract_docx_text, extract_txt_text
from .pdf_parser import extract_pdf_text


logger = logging.getLogger(__name__)
MAX_PDFS = 5
MAX_PAGES_PER_PDF = 25
MAX_TOTAL_PAGES = 75
MAX_TOTAL_CHARS = 120_000
MAX_INPUT_TOKENS_ESTIMATE = 25_000
PDF_REQUEST_TIMEOUT = (5, 30)
PAGE_REQUEST_TIMEOUT = (5, 20)
MAX_PDF_BYTES = 15 * 1024 * 1024
SAM_PUBLIC_RESOURCES_TEMPLATE = "https://sam.gov/api/prod/opps/v3/opportunities/{notice_id}/resources"
SAM_PUBLIC_DOWNLOAD_TEMPLATE = "https://sam.gov/api/prod/opps/v3/opportunities/resources/files/{resource_id}/download"
HIGH_PRIORITY_TERMS = (
    "performance work statement",
    "statement of work",
    "solicitation",
    "rfp",
    "rfq",
    "amendment",
    "pws",
    "sow",
    "soo",
    "statement_of_work",
    "instructions",
    "evaluation",
    "proposal",
    "pricing",
    "requirements",
    "q&a",
    "qanda",
    "qa",
)
LOW_PRIORITY_TERMS = (
    "wage determination",
    "wage_determination",
    "clause",
    "clauses",
    "provision",
    "provisions",
    "attachment",
    "form",
    "forms",
    "template",
    "templates",
    "spreadsheet",
    "admin",
)
SPREADSHEET_TERMS = ("xlsx", "xls", "spreadsheet")
EXTRACTABLE_FILE_KINDS = {"pdf", "docx", "doc", "txt"}


def _safe_filename(url: str, fallback_prefix: str = "solicitation") -> str:
    path = urlparse(url).path.strip("/")
    candidate = os.path.basename(path) or fallback_prefix
    return candidate[:180]


def _fetch_html(url: str) -> str | None:
    if not _is_url_like(url):
        return None

    try:
        resp = requests.get(
            url,
            timeout=PAGE_REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        logger.info("Document discovery request status=%s url=%s", resp.status_code, url)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        logger.warning("Document discovery failed url=%s error=%s", url, repr(exc))
        return None


def _extract_notice_id(opportunity) -> str | None:
    direct = getattr(opportunity, "sam_notice_id", None)
    if direct:
        return str(direct).strip()

    sam_url = getattr(opportunity, "sam_url", None)
    if not _is_url_like(sam_url):
        return None

    match = re.search(r"/opp/([A-Za-z0-9]+)/view", sam_url)
    if match:
        return match.group(1)
    return None


def _public_resource_download_url(resource_id: str) -> str:
    return SAM_PUBLIC_DOWNLOAD_TEMPLATE.format(resource_id=resource_id)


def _empty_summary() -> dict:
    return {
        "total_attachments_found": 0,
        "pdf_candidates_found": 0,
        "doc_candidates_found": 0,
        "txt_candidates_found": 0,
        "spreadsheet_candidates_found": 0,
        "pdfs_processed": 0,
        "docs_processed": 0,
        "txts_processed": 0,
        "spreadsheets_skipped": 0,
        "non_pdfs_skipped": 0,
        "non_extractable_skipped": 0,
        "controlled_or_unavailable_skipped": 0,
        "pdfs_skipped_due_to_limits": 0,
        "documents_skipped_due_to_limits": 0,
        "extraction_failures": 0,
        "pages_extracted": 0,
        "total_extracted_characters": 0,
        "discovery_method": None,
        "max_pdfs": MAX_PDFS,
        "max_pages_per_pdf": MAX_PAGES_PER_PDF,
        "max_total_pages": MAX_TOTAL_PAGES,
        "max_total_chars": MAX_TOTAL_CHARS,
        "max_input_tokens_estimate": MAX_INPUT_TOKENS_ESTIMATE,
    }


def _estimated_tokens(char_count: int) -> int:
    return max(1, (char_count + 3) // 4) if char_count > 0 else 0


def _classify_attachment(filename: str, mime_type: str) -> str:
    name = (filename or "").strip().lower()
    mime = (mime_type or "").strip().lower()

    if name.endswith(".pdf") or "pdf" in mime:
        return "pdf"
    if (
        name.endswith(".docx")
        or "wordprocessingml.document" in mime
        or "application/vnd.openxmlformats-officedocument.wordprocessingml.document" in mime
    ):
        return "docx"
    if name.endswith(".doc") or mime == "application/msword":
        return "doc"
    if name.endswith(".txt") or mime.startswith("text/plain"):
        return "txt"
    if (
        name.endswith(".xlsx")
        or name.endswith(".xls")
        or "spreadsheetml" in mime
        or "application/vnd.ms-excel" in mime
    ):
        return "spreadsheet"
    return "other"


def _rank_resource(resource: dict) -> tuple[int, list[str]]:
    name = str(resource.get("filename", "")).lower()
    score = 0
    reasons: list[str] = []

    for term in HIGH_PRIORITY_TERMS:
        if term in name:
            score += 5
            reasons.append(f"+{term}")
    for term in LOW_PRIORITY_TERMS:
        if term in name:
            score -= 3
            reasons.append(f"-{term}")

    file_kind = resource.get("file_kind")
    if file_kind == "pdf":
        score += 1
        reasons.append("+pdf")
    elif file_kind in {"docx", "doc", "txt"}:
        score += 1
        reasons.append(f"+{file_kind}")
    elif file_kind == "spreadsheet":
        score -= 2
        reasons.append("-spreadsheet")
    elif file_kind == "other":
        score -= 4
        reasons.append("-other")

    posted_date = resource.get("posted_date")
    if posted_date:
        score += 1
        reasons.append("+dated")

    size = resource.get("size")
    if isinstance(size, int) and size > 0:
        if size < 2_000_000:
            score += 1
            reasons.append("+reasonable_size")
        else:
            score -= 1
            reasons.append("-large")

    return score, reasons


def _prioritize_resources(resources: list[dict]) -> list[dict]:
    ranked: list[tuple[int, int, dict, list[str]]] = []
    for idx, resource in enumerate(resources):
        score, reasons = _rank_resource(resource)
        ranked.append((score, -idx, resource, reasons))
        logger.info(
            "Attachment ranking filename=%s score=%s reasons=%s",
            resource.get("filename"),
            score,
            ",".join(reasons) if reasons else "neutral",
        )

    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [resource for _score, _idx, resource, _reasons in ranked]


def _fetch_public_file_resources(opportunity) -> tuple[list[dict], dict]:
    summary = _empty_summary()
    notice_id = _extract_notice_id(opportunity)
    if not notice_id:
        return [], summary

    try:
        resp = requests.get(
            SAM_PUBLIC_RESOURCES_TEMPLATE.format(notice_id=notice_id),
            params={"excludeDeleted": "false", "withScanResult": "false"},
            timeout=PAGE_REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        logger.info(
            "SAM public resources request status=%s opp_id=%s notice_id=%s",
            resp.status_code,
            getattr(opportunity, "id", None),
            notice_id,
        )
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as exc:
        logger.warning(
            "SAM public resources request failed opp_id=%s notice_id=%s error=%s",
            getattr(opportunity, "id", None),
            notice_id,
            repr(exc),
        )
        return [], summary
    except ValueError as exc:
        logger.warning(
            "SAM public resources payload invalid opp_id=%s notice_id=%s error=%s",
            getattr(opportunity, "id", None),
            notice_id,
            repr(exc),
        )
        return [], summary

    attachment_groups = payload.get("_embedded", {}).get("opportunityAttachmentList", [])
    attachments: list[dict] = []
    for group in attachment_groups:
        attachments.extend(group.get("attachments", []))
    summary["total_attachments_found"] = len(attachments)
    summary["discovery_method"] = "sam_public_resources"

    resources: list[dict] = []
    for attachment in attachments:
        deleted = str(attachment.get("deletedFlag", "")).strip()
        access_level = str(attachment.get("accessLevel", "")).strip().lower()
        access_status = str(attachment.get("accessStatus", "")).strip().lower()
        file_exists = str(attachment.get("fileExists", "")).strip()

        if deleted not in ("", "0"):
            continue
        if str(attachment.get("type", "")).lower() != "file":
            continue
        if file_exists not in ("", "1"):
            summary["controlled_or_unavailable_skipped"] += 1
            continue
        if access_level and access_level != "public":
            summary["controlled_or_unavailable_skipped"] += 1
            continue
        if access_status and access_status not in ("public", "approved"):
            summary["controlled_or_unavailable_skipped"] += 1
            continue
        mime_type = str(attachment.get("mimeType", "")).lower()
        name = str(attachment.get("name", "")).strip()
        file_kind = _classify_attachment(name, mime_type)
        resource_id = str(attachment.get("resourceId", "")).strip()
        if not resource_id:
            summary["controlled_or_unavailable_skipped"] += 1
            continue

        if file_kind == "pdf":
            summary["pdf_candidates_found"] += 1
        elif file_kind in {"docx", "doc"}:
            summary["doc_candidates_found"] += 1
        elif file_kind == "txt":
            summary["txt_candidates_found"] += 1
        elif file_kind == "spreadsheet":
            summary["spreadsheet_candidates_found"] += 1
        else:
            summary["non_pdfs_skipped"] += 1

        resources.append(
            {
                "filename": name or _safe_filename(resource_id, fallback_prefix=notice_id),
                "source_url": _public_resource_download_url(resource_id),
                "resource_id": resource_id,
                "access_level": attachment.get("accessLevel"),
                "access_status": attachment.get("accessStatus"),
                "size": attachment.get("size"),
                "posted_date": attachment.get("postedDate"),
                "content_type": mime_type or None,
                "file_kind": file_kind,
            }
        )

    logger.info(
        "SAM public resources summary opp_id=%s notice_id=%s attachments=%s pdf_candidates=%s doc_candidates=%s txt_candidates=%s spreadsheet_candidates=%s non_pdf_skipped=%s controlled_or_unavailable=%s",
        getattr(opportunity, "id", None),
        notice_id,
        summary["total_attachments_found"],
        summary["pdf_candidates_found"],
        summary["doc_candidates_found"],
        summary["txt_candidates_found"],
        summary["spreadsheet_candidates_found"],
        summary["non_pdfs_skipped"],
        summary["controlled_or_unavailable_skipped"],
    )
    return _prioritize_resources(resources), summary


def fetch_opportunity_attachment_metadata(opportunity) -> dict:
    """Return lightweight SAM attachment/resource metadata without downloading files."""
    resources, summary = _fetch_public_file_resources(opportunity)
    attachments = [
        {
            "filename": resource.get("filename"),
            "url": resource.get("source_url"),
            "content_type": resource.get("content_type"),
            "file_kind": resource.get("file_kind"),
            "source": "sam",
        }
        for resource in resources
    ]
    logger.info(
        "Attachment metadata ready opp_id=%s attachments=%s total_found=%s pdf_candidates=%s doc_candidates=%s txt_candidates=%s",
        getattr(opportunity, "id", None),
        len(attachments),
        summary.get("total_attachments_found", 0),
        summary.get("pdf_candidates_found", 0),
        summary.get("doc_candidates_found", 0),
        summary.get("txt_candidates_found", 0),
    )
    return {
        "attachments": attachments,
        "summary": summary,
    }


def _extract_pdf_links_from_html(html: str, base_url: str) -> list[str]:
    links: list[str] = []

    for href in re.findall(r'<a[^>]+href=["\']([^"\']+)["\']', html, re.I):
        resolved = urljoin(base_url, unescape(href))
        if ".pdf" in resolved.lower():
            links.append(resolved)

    for raw_url in re.findall(r'https?://[^"\'>\s]+\.pdf(?:\?[^"\'>\s]*)?', html, re.I):
        links.append(unescape(raw_url))

    seen: set[str] = set()
    deduped: list[str] = []
    for link in links:
        cleaned = link.strip()
        if cleaned in seen:
            continue
        seen.add(cleaned)
        deduped.append(cleaned)

    return deduped[:MAX_PDFS]


def _download_attachment(url: str) -> bytes | None:
    try:
        with requests.get(
            url,
            timeout=PDF_REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
            stream=True,
        ) as resp:
            logger.info("Attachment download status=%s url=%s", resp.status_code, url)
            resp.raise_for_status()

            content_length = resp.headers.get("Content-Length")
            if content_length:
                try:
                    length = int(content_length)
                except ValueError:
                    length = None
                else:
                    if length > MAX_PDF_BYTES:
                        logger.warning("Skipping oversized attachment url=%s bytes=%s", url, length)
                        return None

            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_PDF_BYTES:
                    logger.warning("Skipping oversized streamed attachment url=%s bytes>%s", url, MAX_PDF_BYTES)
                    return None
                chunks.append(chunk)

            return b"".join(chunks)
    except requests.RequestException as exc:
        logger.warning("Attachment download failed url=%s error=%s", url, repr(exc))
        return None


def fetch_opportunity_documents(opportunity) -> dict:
    page_urls = [url for url in [getattr(opportunity, "sam_url", None)] if _is_url_like(url)]

    resources, summary = _fetch_public_file_resources(opportunity)
    if not resources and page_urls:
        pdf_links: list[str] = []
        for page_url in page_urls:
            html = _fetch_html(page_url)
            if not html:
                continue
            discovered = _extract_pdf_links_from_html(html, page_url)
            if discovered:
                logger.info(
                    "HTML fallback found %s PDF link(s) for opp_id=%s",
                    len(discovered),
                    getattr(opportunity, "id", None),
                )
            pdf_links.extend(discovered)

        seen_links: set[str] = set()
        unique_links: list[str] = []
        for link in pdf_links:
            if link in seen_links:
                continue
            seen_links.add(link)
            unique_links.append(link)
        resources = [
            {
                "filename": _safe_filename(link, fallback_prefix=str(getattr(opportunity, "sam_notice_id", "solicitation"))),
                "source_url": link,
                "content_type": "application/pdf",
                "file_kind": "pdf",
            }
            for link in unique_links[:MAX_PDFS]
        ]
        summary["discovery_method"] = "html_fallback"
        summary["pdf_candidates_found"] = len(resources)
        resources = _prioritize_resources(resources)
    elif not resources:
        logger.info("No SAM page URL available for document discovery opp_id=%s", getattr(opportunity, "id", None))
        return {"documents": [], "summary": summary}

    documents: list[dict] = []
    for index, resource in enumerate(resources):
        if len(documents) >= MAX_PDFS:
            skipped_remaining = max(0, len(resources) - index)
            summary["pdfs_skipped_due_to_limits"] += skipped_remaining
            summary["documents_skipped_due_to_limits"] += skipped_remaining
            logger.info("Document cap reached opp_id=%s max_docs=%s", getattr(opportunity, "id", None), MAX_PDFS)
            break

        remaining_pages = MAX_TOTAL_PAGES - summary["pages_extracted"]
        remaining_chars = MAX_TOTAL_CHARS - summary["total_extracted_characters"]
        if remaining_pages <= 0 or remaining_chars <= 0:
            skipped_remaining = max(0, len(resources) - index)
            summary["pdfs_skipped_due_to_limits"] += skipped_remaining
            summary["documents_skipped_due_to_limits"] += skipped_remaining
            logger.info(
                "Global extraction cap reached opp_id=%s remaining_pages=%s remaining_chars=%s",
                getattr(opportunity, "id", None),
                remaining_pages,
                remaining_chars,
            )
            break

        filename = resource["filename"]
        file_kind = resource.get("file_kind") or "other"
        if file_kind == "spreadsheet":
            summary["spreadsheets_skipped"] += 1
            logger.info("Skipping spreadsheet attachment filename=%s", filename)
            continue
        if file_kind not in EXTRACTABLE_FILE_KINDS:
            summary["non_extractable_skipped"] += 1
            logger.info("Skipping non-extractable attachment filename=%s file_kind=%s", filename, file_kind)
            continue

        file_bytes = _download_attachment(resource["source_url"])
        if not file_bytes:
            summary["extraction_failures"] += 1
            continue

        if file_kind == "pdf":
            parsed = extract_pdf_text(
                file_bytes,
                filename=filename,
                max_pages=min(MAX_PAGES_PER_PDF, remaining_pages),
                max_chars=remaining_chars,
            )
        elif file_kind == "docx":
            parsed = extract_docx_text(file_bytes, filename=filename, max_chars=remaining_chars)
        elif file_kind == "doc":
            parsed = extract_doc_text(file_bytes, filename=filename, max_chars=remaining_chars)
        else:
            parsed = extract_txt_text(file_bytes, filename=filename, max_chars=remaining_chars)

        if not parsed:
            logger.warning("Skipping attachment with no extracted text filename=%s url=%s file_kind=%s", filename, resource["source_url"], file_kind)
            summary["extraction_failures"] += 1
            continue

        documents.append(
            {
                "filename": filename,
                "source_url": resource["source_url"],
                "content_type": resource.get("content_type"),
                "file_kind": file_kind,
                "extracted_text": parsed["extracted_text"],
                "pages_extracted": parsed["pages_extracted"],
                "total_characters": parsed["total_characters"],
            }
        )
        if file_kind == "pdf":
            summary["pdfs_processed"] += 1
        elif file_kind in {"docx", "doc"}:
            summary["docs_processed"] += 1
        else:
            summary["txts_processed"] += 1
        summary["pages_extracted"] += parsed["pages_extracted"]
        summary["total_extracted_characters"] += parsed["total_characters"]

    logger.info(
        "Document fetch complete opp_id=%s method=%s attachments=%s pdf_candidates=%s doc_candidates=%s txt_candidates=%s processed_pdf=%s processed_doc=%s processed_txt=%s skipped_due_to_limits=%s spreadsheets_skipped=%s non_extractable_skipped=%s non_pdf_skipped=%s controlled_or_unavailable=%s extraction_failures=%s pages_extracted=%s total_chars=%s estimated_tokens=%s",
        getattr(opportunity, "id", None),
        summary["discovery_method"],
        summary["total_attachments_found"],
        summary["pdf_candidates_found"],
        summary["doc_candidates_found"],
        summary["txt_candidates_found"],
        summary["pdfs_processed"],
        summary["docs_processed"],
        summary["txts_processed"],
        summary["documents_skipped_due_to_limits"],
        summary["spreadsheets_skipped"],
        summary["non_extractable_skipped"],
        summary["non_pdfs_skipped"],
        summary["controlled_or_unavailable_skipped"],
        summary["extraction_failures"],
        summary["pages_extracted"],
        summary["total_extracted_characters"],
        _estimated_tokens(summary["total_extracted_characters"]),
    )
    return {"documents": documents, "summary": summary}
