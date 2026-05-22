import io
import logging
import re
from typing import Any


logger = logging.getLogger(__name__)
MAX_PDF_PAGES = 25
MAX_PDF_CHARS = 120_000


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def extract_pdf_text(
    pdf_bytes: bytes,
    *,
    filename: str,
    max_pages: int = MAX_PDF_PAGES,
    max_chars: int = MAX_PDF_CHARS,
) -> dict[str, Any] | None:
    try:
        from pypdf import PdfReader
        from pypdf.errors import DependencyError
    except ImportError:
        logger.warning("PDF parser unavailable for %s because pypdf is not installed", filename)
        return None

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as exc:
        logger.warning("PDF parse failed for %s error=%s", filename, repr(exc))
        return None

    try:
        page_count = min(len(reader.pages), max_pages)
    except DependencyError as exc:
        logger.warning(
            "PDF parse skipped for %s because an extra crypto dependency is required error=%s",
            filename,
            repr(exc),
        )
        return None
    except Exception as exc:
        logger.warning("PDF page count failed for %s error=%s", filename, repr(exc))
        return None

    if max_pages <= 0 or max_chars <= 0:
        logger.info("PDF extraction skipped for %s because limits were already exhausted", filename)
        return None

    chunks: list[str] = []
    total_characters = 0
    capped_by_chars = False

    for page_index in range(page_count):
        try:
            page_text = reader.pages[page_index].extract_text() or ""
        except DependencyError as exc:
            logger.warning(
                "PDF page extraction skipped for %s page=%s because an extra crypto dependency is required error=%s",
                filename,
                page_index + 1,
                repr(exc),
            )
            return None
        except Exception as exc:
            logger.warning(
                "PDF page extraction failed for %s page=%s error=%s",
                filename,
                page_index + 1,
                repr(exc),
            )
            continue
        normalized = _normalize_text(page_text)
        if normalized:
            remaining_chars = max_chars - total_characters
            if remaining_chars <= 0:
                capped_by_chars = True
                logger.info("PDF char cap reached before page=%s for %s", page_index + 1, filename)
                break
            if len(normalized) > remaining_chars:
                normalized = normalized[:remaining_chars].rstrip() + "…"
                capped_by_chars = True
            chunks.append(normalized)
            total_characters += len(normalized)
            if capped_by_chars:
                logger.info("PDF char cap reached during page=%s for %s", page_index + 1, filename)
                break

    extracted_text = "\n\n".join(chunks).strip()
    total_characters = len(extracted_text)
    actual_pages = len(chunks)

    if not extracted_text:
        logger.info("PDF extraction produced no readable text for %s", filename)
        return None

    logger.info(
        "PDF extraction succeeded filename=%s pages_extracted=%s total_characters=%s capped_by_chars=%s",
        filename,
        actual_pages,
        total_characters,
        capped_by_chars,
    )

    return {
        "extracted_text": extracted_text,
        "pages_extracted": actual_pages,
        "total_characters": total_characters,
        "capped_by_chars": capped_by_chars,
    }
