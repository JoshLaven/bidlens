"""Neutral, deterministic cleanup for stored communication content."""

from __future__ import annotations

from html.parser import HTMLParser
import re


ACKNOWLEDGMENT_ONLY = frozenset({
    "acknowledged", "got it", "noted", "received", "thank you", "thanks", "thanks!",
})
AUTOMATED_SUBJECT_PATTERNS = (
    re.compile(r"\b(out of office|automatic reply|auto(?:matic)? response)\b", re.I),
    re.compile(r"\b(delivery (?:status )?notification|undeliverable|non-delivery report|mail delivery failed)\b", re.I),
    re.compile(r"\b(subscription|unsubscribe|system notification)\b", re.I),
)
AUTOMATED_BODY_PATTERNS = (
    re.compile(r"\bi am (?:currently )?out of (?:the )?office\b", re.I),
    re.compile(r"\bdelivery (?:to .* )?(?:has failed|was unsuccessful)\b", re.I),
    re.compile(r"\bthis is an automated (?:message|notification|response)\b", re.I),
)
SIGNATURE_ONLY_PATTERN = re.compile(
    r"^(?:best|best regards|kind regards|regards|sincerely),?\s+[\w.' -]{1,100}$", re.I,
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style"}:
            self.hidden_depth += 1
        elif not self.hidden_depth and tag.lower() in {"br", "p", "div", "li", "blockquote", "hr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self.hidden_depth:
            self.hidden_depth -= 1
        elif not self.hidden_depth and tag.lower() in {"p", "div", "li", "blockquote"}:
            self.parts.append("\n")


def plain_text(body: str | None, content_type: str | None = None) -> str:
    value = body or ""
    if "html" in (content_type or "").lower() or re.search(r"</?[a-z][^>]*>", value, re.I):
        parser = _TextExtractor()
        try:
            parser.feed(value)
            value = "".join(parser.parts)
        except Exception:
            value = re.sub(r"<[^>]+>", " ", value)
    return value


def clean_message_body(body: str | None, content_type: str | None = None) -> str:
    """Preserve the proven Communication Summary cleanup behavior exactly."""
    lines = plain_text(body, content_type).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(">"):
            break
        if re.match(r"^On .+ wrote:$", stripped, re.I):
            break
        if re.match(r"^-{2,}\s*(Original Message|Forwarded message)\s*-{2,}$", stripped, re.I):
            break
        if re.match(r"^(From|Sent|To|Subject):\s+", stripped, re.I) and kept:
            break
        if re.match(r"^(Get Outlook for|Sent from my (iPhone|iPad|Android))", stripped, re.I):
            continue
        if stripped in {"--", "-- "}:
            break
        if kept and re.match(r"^(Best|Best regards|Kind regards|Regards|Sincerely),?$", stripped, re.I):
            break
        kept.append(stripped)
    return re.sub(r"\s+", " ", " ".join(kept)).strip()


def non_substantive_message_reason(*, cleaned_body: str, subject: str = "") -> str | None:
    """Return GUTS' existing deterministic exclusion reason, without semantics."""
    if not cleaned_body:
        return "empty_original_content"
    if SIGNATURE_ONLY_PATTERN.fullmatch(cleaned_body):
        return "signature_only"
    if cleaned_body.casefold().strip(" .,!?:;") in ACKNOWLEDGMENT_ONLY:
        return "acknowledgment_only"
    if any(pattern.search(subject) for pattern in AUTOMATED_SUBJECT_PATTERNS) or any(
        pattern.search(cleaned_body) for pattern in AUTOMATED_BODY_PATTERNS
    ):
        return "automated_message"
    return None
