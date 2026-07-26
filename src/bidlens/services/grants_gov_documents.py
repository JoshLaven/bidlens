from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


GRANTS_GOV_SOURCES = {"grants_gov", "grants.gov"}
GRANTS_GOV_ATTACHMENT_DOWNLOAD_TEMPLATE = (
    "https://www.grants.gov/grantsws/rest/opportunity/att/download/{attachment_id}"
)


def is_grants_gov_opportunity(opportunity: Any) -> bool:
    return str(getattr(opportunity, "source", "") or "").strip().lower() in GRANTS_GOV_SOURCES


def grants_gov_attachment_download_url(attachment_id: Any) -> str | None:
    if attachment_id in (None, ""):
        return None
    return GRANTS_GOV_ATTACHMENT_DOWNLOAD_TEMPLATE.format(attachment_id=attachment_id)


def _raw_path(payload: dict[str, Any], *path: str) -> Any:
    value: Any = payload
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _first_raw_value(payload: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        value = _raw_path(payload, *path)
        if value not in (None, ""):
            return value
    return None


def _coerce_document_url(value: Any) -> dict[str, str | None] | None:
    if isinstance(value, str):
        text = value.strip()
        return {"label": text, "url": text} if text else None
    if not isinstance(value, dict):
        return None
    url = (
        value.get("url")
        or value.get("URL")
        or value.get("link")
        or value.get("href")
        or value.get("documentUrl")
        or value.get("documentURL")
        or value.get("docUrl")
    )
    label = (
        value.get("label")
        or value.get("name")
        or value.get("title")
        or value.get("description")
        or value.get("fileName")
        or url
    )
    if not url and not label:
        return None
    return {
        "label": str(label).strip() if label else None,
        "url": str(url).strip() if url else None,
    }


def grants_gov_document_metadata(opportunity: Any) -> dict[str, list[dict[str, Any]]]:
    """Normalize stored Grants.gov attachment metadata for UI and acquisition."""
    raw = getattr(opportunity, "raw_source_payload", None)
    if not is_grants_gov_opportunity(opportunity) or not isinstance(raw, dict):
        return {"folders": [], "document_urls": []}

    folders = _first_raw_value(
        raw,
        ("synopsisAttachmentFolders",),
        ("detail_payload", "data", "synopsisAttachmentFolders"),
    )
    normalized_folders: list[dict[str, Any]] = []
    if isinstance(folders, list):
        for folder in folders:
            if not isinstance(folder, dict):
                continue
            attachments: list[dict[str, Any]] = []
            for attachment in folder.get("synopsisAttachments") or []:
                if not isinstance(attachment, dict):
                    continue
                attachment_id = attachment.get("id")
                file_name = attachment.get("fileName")
                file_description = attachment.get("fileDescription")
                mime_type = attachment.get("mimeType")
                file_size = attachment.get("fileLobSize")
                if any((attachment_id, file_name, file_description, mime_type, file_size)):
                    attachments.append(
                        {
                            "id": attachment_id,
                            "file_name": file_name,
                            "file_description": file_description,
                            "mime_type": mime_type,
                            "file_size": file_size,
                            "download_url": grants_gov_attachment_download_url(attachment_id),
                        }
                    )
            if attachments:
                normalized_folders.append(
                    {
                        "folder_name": folder.get("folderName"),
                        "folder_type": folder.get("folderType"),
                        "zip_size": folder.get("zipLobSize"),
                        "attachments": attachments,
                    }
                )

    document_urls = _first_raw_value(
        raw,
        ("synopsisDocumentURLs",),
        ("detail_payload", "data", "synopsisDocumentURLs"),
    )
    normalized_urls: list[dict[str, str | None]] = []
    if isinstance(document_urls, list):
        for document_url in document_urls:
            normalized = _coerce_document_url(document_url)
            if normalized:
                normalized_urls.append(normalized)

    return {"folders": normalized_folders, "document_urls": normalized_urls}


def grants_gov_document_resources(opportunity: Any) -> list[dict[str, Any]]:
    metadata = grants_gov_document_metadata(opportunity)
    resources: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for folder in metadata["folders"]:
        for attachment in folder["attachments"]:
            url = attachment.get("download_url")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            resources.append(
                {
                    "filename": attachment.get("file_name") or f"attachment-{attachment.get('id')}",
                    "source_url": url,
                    "content_type": attachment.get("mime_type"),
                    "size": attachment.get("file_size"),
                    "source": "grants_gov",
                }
            )

    for document in metadata["document_urls"]:
        url = document.get("url")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        path_name = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        resources.append(
            {
                "filename": document.get("label") or path_name or "grant-document",
                "source_url": url,
                "content_type": None,
                "size": None,
                "source": "grants_gov",
            }
        )
    return resources
