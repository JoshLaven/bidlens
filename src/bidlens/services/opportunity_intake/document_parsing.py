from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePath

from pypdf import PdfReader

from ... import config
from ..research.document_text_parser import extract_docx_text
from ..research.pdf_parser import extract_pdf_text


SUPPORTED_DOCUMENT_MIME_TYPES = {
    ".pdf": {"application/pdf", "application/octet-stream"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
        "application/octet-stream",
    },
}


class IntakeDocumentError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ParsedIntakeDocument:
    extracted_text: str
    parser_type: str
    page_count: int | None
    total_characters: int
    warnings: tuple[str, ...] = ()
    metadata: dict | None = None


def validate_intake_document(
    *, filename: str | None, mime_type: str | None, content: bytes, max_bytes: int | None = None
) -> str:
    suffix = PurePath(str(filename or "").replace("\\", "/")).suffix.lower()
    if suffix == ".doc":
        raise IntakeDocumentError("unsupported_legacy_doc", "Legacy .doc files are not supported. Upload a PDF or DOCX file.")
    if suffix not in SUPPORTED_DOCUMENT_MIME_TYPES:
        raise IntakeDocumentError("unsupported_type", "Upload a PDF or DOCX file.")
    if not content:
        raise IntakeDocumentError("empty_file", "The selected file is empty.")
    limit = config.SOURCE_MATERIAL_MAX_BYTES if max_bytes is None else max_bytes
    if limit <= 0 or len(content) > limit:
        raise IntakeDocumentError("file_too_large", "The selected file exceeds the configured upload limit.")
    normalized_mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    if normalized_mime and normalized_mime not in SUPPORTED_DOCUMENT_MIME_TYPES[suffix]:
        raise IntakeDocumentError("type_mismatch", "The file extension and content type do not match.")
    if suffix == ".pdf" and not content.startswith(b"%PDF-"):
        raise IntakeDocumentError("invalid_pdf", "The selected file is not a valid PDF.")
    if suffix == ".docx" and not content.startswith(b"PK"):
        raise IntakeDocumentError("invalid_docx", "The selected file is not a valid DOCX document.")
    return suffix


def parse_intake_document(
    *, filename: str, mime_type: str | None, content: bytes
) -> ParsedIntakeDocument:
    suffix = validate_intake_document(filename=filename, mime_type=mime_type, content=content)
    if suffix == ".pdf":
        try:
            PdfReader(BytesIO(content))
        except Exception as exc:
            raise IntakeDocumentError(
                "invalid_pdf", "BidLens could not read this PDF document."
            ) from exc
        parsed = extract_pdf_text(
            content,
            filename=filename,
            max_pages=config.INTAKE_DOCUMENT_MAX_PDF_PAGES,
            max_chars=config.INTAKE_DOCUMENT_MAX_TEXT_CHARS,
        )
        if not parsed:
            return ParsedIntakeDocument(
                extracted_text="",
                parser_type="pypdf",
                page_count=None,
                total_characters=0,
                warnings=("No readable text was found. This PDF may be scanned and require OCR.",),
            )
        warnings = ()
        if parsed.get("capped_by_chars"):
            warnings = ("Document text was limited before extraction.",)
        return ParsedIntakeDocument(
            extracted_text=parsed["extracted_text"],
            parser_type="pypdf",
            page_count=parsed.get("pages_extracted"),
            total_characters=parsed["total_characters"],
            warnings=warnings,
            metadata={"capped_by_chars": bool(parsed.get("capped_by_chars"))},
        )
    parsed = extract_docx_text(
        content,
        filename=filename,
        max_chars=config.INTAKE_DOCUMENT_MAX_TEXT_CHARS,
    )
    if not parsed:
        raise IntakeDocumentError("invalid_docx", "BidLens could not read this DOCX document.")
    warnings = ("Document text was limited before extraction.",) if parsed.get("capped_by_chars") else ()
    return ParsedIntakeDocument(
        extracted_text=parsed["extracted_text"],
        parser_type="docx_xml",
        page_count=None,
        total_characters=parsed["total_characters"],
        warnings=warnings,
        metadata={"capped_by_chars": bool(parsed.get("capped_by_chars"))},
    )
