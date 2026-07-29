from __future__ import annotations

import re
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import PurePath
from time import perf_counter

from ... import config


EMAIL_MIME_TYPES = {"message/rfc822", "application/octet-stream", "text/plain"}
_REPLY_MARKER = re.compile(r"^On .+ wrote:\s*$", re.IGNORECASE)


class IntakeEmailError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ParsedEmailAttachment:
    filename: str
    mime_type: str
    content: bytes


@dataclass(frozen=True)
class ParsedIntakeEmail:
    subject: str | None
    sender: str | None
    to_recipients: tuple[str, ...]
    cc_recipients: tuple[str, ...]
    sent_at: str | None
    internet_message_id: str | None
    provider: str | None
    provider_message_id: str | None
    body_text: str
    body_source: str | None
    attachments: tuple[ParsedEmailAttachment, ...]
    skipped_attachments: tuple[dict[str, object], ...]
    warnings: tuple[str, ...]


class _HTMLTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        normalized = tag.lower()
        if normalized in {"script", "style", "template", "noscript"}:
            self.hidden_depth += 1
        elif not self.hidden_depth and normalized in {"br", "p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in {"script", "style", "template", "noscript"} and self.hidden_depth:
            self.hidden_depth -= 1
        elif not self.hidden_depth and normalized in {"p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)


def _bounded_text(value: str | None, limit: int) -> str:
    normalized = "\n".join(
        line.strip() for line in str(value or "").replace("\x00", "").splitlines() if line.strip()
    )
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "…"


def _html_to_text(value: str, limit: int) -> str:
    parser = _HTMLTextParser()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        pass
    return _bounded_text("".join(parser.parts), limit)


def _strip_quoted_reply(value: str) -> str:
    kept: list[str] = []
    for line in value.splitlines():
        stripped = line.strip()
        if _REPLY_MARKER.match(stripped):
            break
        if stripped.startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _addresses(message, header: str) -> tuple[str, ...]:
    values = message.get_all(header, [])
    results: list[str] = []
    for name, address in getaddresses([str(value) for value in values]):
        clean_name = _bounded_text(name, 160)
        clean_address = _bounded_text(address, 254)
        value = clean_address or clean_name
        if value and value not in results:
            results.append(value)
    return tuple(results)


def _sent_at(message) -> tuple[str | None, str | None]:
    raw = message.get("Date")
    if not raw:
        return None, None
    try:
        parsed = parsedate_to_datetime(str(raw))
        return parsed.isoformat(), None
    except (TypeError, ValueError, OverflowError):
        return None, "The email sent date could not be read."


def _mime_parts(part, depth: int = 0):
    if depth > 25:
        return
    if part.get_content_type().lower() == "message/rfc822":
        yield part
        return
    if part.is_multipart():
        for child in part.iter_parts():
            yield from _mime_parts(child, depth + 1)
        return
    yield part


def validate_intake_email(
    *, filename: str | None, mime_type: str | None, content: bytes, max_bytes: int | None = None
) -> None:
    suffix = PurePath(str(filename or "").replace("\\", "/")).suffix.lower()
    if suffix == ".msg":
        raise IntakeEmailError("unsupported_msg", "Outlook .msg files are not supported yet. Export or save the email as .eml.")
    if suffix != ".eml":
        raise IntakeEmailError("unsupported_type", "Upload an email file in .eml format.")
    if not content:
        raise IntakeEmailError("empty_file", "The selected email file is empty.")
    limit = config.SOURCE_MATERIAL_MAX_BYTES if max_bytes is None else max_bytes
    if limit <= 0 or len(content) > limit:
        raise IntakeEmailError("file_too_large", "The selected email exceeds the configured upload limit.")
    normalized_mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    if normalized_mime and normalized_mime not in EMAIL_MIME_TYPES:
        raise IntakeEmailError("type_mismatch", "The file extension and content type do not match an .eml file.")


def parse_intake_email(*, filename: str, mime_type: str | None, content: bytes) -> ParsedIntakeEmail:
    validate_intake_email(filename=filename, mime_type=mime_type, content=content)
    warnings: list[str] = []
    try:
        message = BytesParser(policy=policy.default).parsebytes(content)
    except Exception as exc:
        raise IntakeEmailError("invalid_eml", "BidLens could not read this email file.") from exc

    subject = _bounded_text(str(message.get("Subject") or ""), 500) or None
    senders = _addresses(message, "From")
    sender = senders[0] if senders else None
    to_recipients = _addresses(message, "To")
    cc_recipients = _addresses(message, "Cc")
    sent_at, sent_warning = _sent_at(message)
    if sent_warning:
        warnings.append(sent_warning)
    message_id = _bounded_text(str(message.get("Message-ID") or ""), 500) or None
    provider_message_id = _bounded_text(
        str(message.get("X-Microsoft-Original-Message-ID") or message.get("X-MS-Exchange-Message-ID") or ""),
        500,
    ) or None
    provider = "microsoft" if provider_message_id or any(
        str(name).lower().startswith("x-ms-exchange-") for name in message.keys()
    ) else None

    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[ParsedEmailAttachment] = []
    skipped: list[dict[str, object]] = []
    total_attachment_bytes = 0
    attachment_count = 0
    mime_started = perf_counter()
    for part in _mime_parts(message):
        if perf_counter() - mime_started > config.INTAKE_EMAIL_MAX_PARSING_SECONDS:
            warnings.append("Email MIME parsing stopped after reaching the configured time limit.")
            break
        content_type = part.get_content_type().lower()
        disposition = part.get_content_disposition()
        filename_value = part.get_filename()
        is_attachment = disposition == "attachment" or bool(filename_value)
        if is_attachment:
            attachment_count += 1
            safe_filename = _bounded_text(str(filename_value or f"attachment-{attachment_count}"), 180)
            if attachment_count > config.INTAKE_EMAIL_MAX_ATTACHMENTS:
                skipped.append({"filename": safe_filename, "reason": "attachment_count_limit"})
                continue
            try:
                payload = part.get_payload(decode=True) or b""
            except Exception:
                payload = b""
            if len(payload) > config.INTAKE_EMAIL_MAX_ATTACHMENT_BYTES:
                skipped.append({"filename": safe_filename, "reason": "attachment_size_limit"})
                continue
            if total_attachment_bytes + len(payload) > config.INTAKE_EMAIL_MAX_TOTAL_ATTACHMENT_BYTES:
                skipped.append({"filename": safe_filename, "reason": "total_attachment_size_limit"})
                continue
            total_attachment_bytes += len(payload)
            suffix = PurePath(safe_filename).suffix.lower()
            if suffix not in {".pdf", ".docx"}:
                skipped.append({"filename": safe_filename, "mime_type": content_type, "byte_size": len(payload), "reason": "unsupported_type"})
                continue
            attachments.append(ParsedEmailAttachment(safe_filename, content_type, payload))
            continue
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            decoded = part.get_content()
        except Exception:
            raw = part.get_payload(decode=True) or b""
            decoded = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
        if not isinstance(decoded, str):
            continue
        if content_type == "text/plain":
            plain_parts.append(decoded)
        else:
            html_parts.append(decoded)

    if plain_parts:
        body = _bounded_text(_strip_quoted_reply("\n\n".join(plain_parts)), config.INTAKE_EMAIL_MAX_BODY_CHARS)
        body_source = "plain"
    elif html_parts:
        body = _html_to_text("\n\n".join(html_parts), config.INTAKE_EMAIL_MAX_BODY_CHARS)
        body = _bounded_text(_strip_quoted_reply(body), config.INTAKE_EMAIL_MAX_BODY_CHARS)
        body_source = "html"
    else:
        body, body_source = "", None
        warnings.append("No readable email body was found.")
    if skipped:
        warnings.append(f"{len(skipped)} attachment(s) were skipped because they were unsupported or exceeded intake limits.")
    if not any((subject, sender, to_recipients, cc_recipients, body, attachments)):
        warnings.append("The email contained very little readable information. Enter the opportunity details manually.")
    return ParsedIntakeEmail(
        subject=subject,
        sender=sender,
        to_recipients=to_recipients,
        cc_recipients=cc_recipients,
        sent_at=sent_at,
        internet_message_id=message_id,
        provider=provider,
        provider_message_id=provider_message_id,
        body_text=body,
        body_source=body_source,
        attachments=tuple(attachments),
        skipped_attachments=tuple(skipped),
        warnings=tuple(warnings),
    )
