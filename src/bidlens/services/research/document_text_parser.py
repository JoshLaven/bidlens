import io
import logging
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree


logger = logging.getLogger(__name__)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    normalized = _normalize_text(text)
    if not normalized:
        return "", False
    if max_chars <= 0:
        return "", False
    if len(normalized) <= max_chars:
        return normalized, False
    return normalized[: max_chars - 1].rstrip() + "…", True


def extract_docx_text(
    file_bytes: bytes,
    *,
    filename: str,
    max_chars: int,
) -> dict | None:
    if max_chars <= 0:
        logger.info("DOCX extraction skipped for %s because limits were already exhausted", filename)
        return None

    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            parts = [
                "word/document.xml",
                "word/header1.xml",
                "word/header2.xml",
                "word/footer1.xml",
                "word/footer2.xml",
                "word/footnotes.xml",
                "word/endnotes.xml",
            ]
            texts: list[str] = []
            for part in parts:
                try:
                    raw = archive.read(part)
                except KeyError:
                    continue
                try:
                    root = ElementTree.fromstring(raw)
                except ElementTree.ParseError:
                    continue
                text_nodes = []
                for element in root.iter():
                    if element.tag.endswith("}t") and element.text:
                        text_nodes.append(element.text)
                if text_nodes:
                    texts.append(" ".join(text_nodes))
    except zipfile.BadZipFile:
        logger.warning("DOCX parse failed for %s because the file is not a valid zip archive", filename)
        return None
    except Exception as exc:
        logger.warning("DOCX parse failed for %s error=%s", filename, repr(exc))
        return None

    combined = "\n\n".join(texts).strip()
    extracted_text, capped_by_chars = _truncate_text(combined, max_chars)
    if not extracted_text:
        logger.info("DOCX extraction produced no readable text for %s", filename)
        return None

    logger.info(
        "DOCX extraction succeeded filename=%s total_characters=%s capped_by_chars=%s",
        filename,
        len(extracted_text),
        capped_by_chars,
    )
    return {
        "extracted_text": extracted_text,
        "pages_extracted": 0,
        "total_characters": len(extracted_text),
        "capped_by_chars": capped_by_chars,
    }


def extract_doc_text(
    file_bytes: bytes,
    *,
    filename: str,
    max_chars: int,
) -> dict | None:
    if max_chars <= 0:
        logger.info("DOC extraction skipped for %s because limits were already exhausted", filename)
        return None

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / filename
            path.write_bytes(file_bytes)
            result = subprocess.run(
                ["textutil", "-convert", "txt", "-stdout", str(path)],
                capture_output=True,
                timeout=20,
                check=False,
            )
    except FileNotFoundError:
        logger.warning("DOC extraction unavailable for %s because textutil is not installed", filename)
        return None
    except subprocess.TimeoutExpired as exc:
        logger.warning("DOC extraction timed out for %s error=%s", filename, repr(exc))
        return None
    except Exception as exc:
        logger.warning("DOC extraction failed for %s error=%s", filename, repr(exc))
        return None

    if result.returncode != 0 or not result.stdout:
        logger.warning("DOC extraction failed for %s returncode=%s", filename, result.returncode)
        return None

    text = result.stdout.decode("utf-8", errors="replace")
    extracted_text, capped_by_chars = _truncate_text(text, max_chars)
    if not extracted_text:
        logger.info("DOC extraction produced no readable text for %s", filename)
        return None

    logger.info(
        "DOC extraction succeeded filename=%s total_characters=%s capped_by_chars=%s",
        filename,
        len(extracted_text),
        capped_by_chars,
    )
    return {
        "extracted_text": extracted_text,
        "pages_extracted": 0,
        "total_characters": len(extracted_text),
        "capped_by_chars": capped_by_chars,
    }


def extract_txt_text(
    file_bytes: bytes,
    *,
    filename: str,
    max_chars: int,
) -> dict | None:
    if max_chars <= 0:
        logger.info("TXT extraction skipped for %s because limits were already exhausted", filename)
        return None

    decoded = None
    for encoding in ("utf-8", "utf-16", "utf-16-le", "utf-16-be", "latin-1"):
        try:
            decoded = file_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue

    if decoded is None:
        logger.warning("TXT extraction failed for %s because no supported encoding matched", filename)
        return None

    extracted_text, capped_by_chars = _truncate_text(decoded, max_chars)
    if not extracted_text:
        logger.info("TXT extraction produced no readable text for %s", filename)
        return None

    logger.info(
        "TXT extraction succeeded filename=%s total_characters=%s capped_by_chars=%s",
        filename,
        len(extracted_text),
        capped_by_chars,
    )
    return {
        "extracted_text": extracted_text,
        "pages_extracted": 0,
        "total_characters": len(extracted_text),
        "capped_by_chars": capped_by_chars,
    }
