from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import logging
import re
from time import perf_counter
from typing import Any, Protocol

from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session
from sqlalchemy import func

from .. import config
from ..models import Opportunity, OpportunityCommunicationMessage, OpportunityCommunicationSummary, User, Workspace
from .opportunity_conversations import deduplicate_communication_messages

logger = logging.getLogger(__name__)
WAITING_ON_VALUES = {"our_team", "external_party", "both", "nobody", "unclear"}


class CommunicationSummaryError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
    def handle_data(self, data: str) -> None:
        self.parts.append(data)
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"br", "p", "div", "li", "blockquote", "hr"}:
            self.parts.append("\n")
    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"p", "div", "li", "blockquote"}:
            self.parts.append("\n")


@dataclass(frozen=True)
class PreparedCommunicationInput:
    text: str
    message_count_included: int
    message_count_available: int
    latest_message_timestamp_included: datetime | None


@dataclass(frozen=True)
class CommunicationSummaryResult:
    current_status: str
    key_updates: list[str]
    open_questions: list[str]
    next_action: str
    waiting_on: str
    provider: str
    model: str
    usage: dict[str, Any]
    timings_ms: dict[str, float] | None = None


class CommunicationSummaryGenerator(Protocol):
    def generate_summary(self, input_data: PreparedCommunicationInput) -> CommunicationSummaryResult: ...


def _plain_text(body: str | None, content_type: str | None) -> str:
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
    lines = _plain_text(body, content_type).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(">"):
            break
        if re.match(r"^On .+ wrote:$", stripped, re.I):
            break
        if re.match(r"^-{2,}\s*(Original Message|Forwarded message)\s*-{2,}$", stripped, re.I):
            break
        if re.match(r"^(From|Sent|To|Subject):\s+", stripped, re.I) and len(kept) > 0:
            break
        if re.match(r"^(Get Outlook for|Sent from my (iPhone|iPad|Android))", stripped, re.I):
            continue
        if stripped == "--" or stripped == "-- ":
            break
        kept.append(stripped)
    return re.sub(r"\s+", " ", " ".join(kept)).strip()


def _recipient_addresses(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    addresses = []
    for item in value:
        if isinstance(item, dict):
            address = str(item.get("address") or "").strip()
            name = str(item.get("name") or "").strip()
            if address:
                addresses.append(f"{name} <{address}>" if name else address)
        elif str(item).strip():
            addresses.append(str(item).strip())
    return ", ".join(addresses)


def select_messages(db: Session, *, workspace_id: int, opportunity_id: int) -> list[OpportunityCommunicationMessage]:
    return deduplicate_communication_messages(
        (
        db.query(OpportunityCommunicationMessage)
        .filter(
            OpportunityCommunicationMessage.workspace_id == workspace_id,
            OpportunityCommunicationMessage.opportunity_id == opportunity_id,
        )
        .order_by(
            func.coalesce(OpportunityCommunicationMessage.provider_timestamp, OpportunityCommunicationMessage.created_at).asc(),
            OpportunityCommunicationMessage.id.asc(),
        )
        .all()
        )
    )


def _render_message(message: OpportunityCommunicationMessage) -> str:
    timestamp = message.provider_timestamp or message.created_at
    sender = message.sender_address or message.sender_display_name or "Unknown"
    body = clean_message_body(message.body, message.body_content_type) or "(No substantive body text)"
    return "\n".join((
        f"Timestamp: {timestamp.isoformat() if timestamp else 'Unknown'}",
        f"Direction: {message.direction if message.direction in {'inbound', 'outbound'} else 'unknown'}",
        f"Sender: {sender}",
        f"Recipients: {_recipient_addresses(message.recipients_json) or 'Unknown'}",
        f"Subject: {message.subject or '(No subject)'}",
        f"Body: {body}",
    ))


def prepare_communication_input(messages: list[OpportunityCommunicationMessage], *, max_chars: int) -> PreparedCommunicationInput:
    if max_chars < 500:
        raise CommunicationSummaryError("invalid_configuration", "The summary input limit is too small.")
    rendered = [_render_message(message) for message in messages]
    selected: list[tuple[int, str]] = []
    used = 0
    # Reserve a small, deterministic slice for the earliest record, then fill
    # newest-first. This preserves grounding while protecting the latest status.
    if len(rendered) > 1:
        earliest_budget = max(100, max_chars // 5)
        earliest_block = rendered[0]
        if len(earliest_block) > earliest_budget:
            head = earliest_budget // 2
            earliest = f"{earliest_block[:head].rstrip()}\n…\n{earliest_block[-(earliest_budget - head - 3):].lstrip()}"
        else:
            earliest = earliest_block
        selected.append((0, earliest))
        used = len(earliest)
    for index in range(len(rendered) - 1, (0 if len(rendered) > 1 else -1), -1):
        block = rendered[index]
        separator_cost = 2 if selected else 0
        if used + separator_cost + len(block) <= max_chars:
            selected.append((index, block))
            used += separator_cost + len(block)
        elif index == len(rendered) - 1:
            remaining = max_chars - used - separator_cost
            if remaining > 0:
                selected.append((index, block[:remaining].rstrip()))
                used = max_chars
    selected.sort(key=lambda row: row[0])
    included_indexes = [row[0] for row in selected]
    latest = None
    if included_indexes:
        message = messages[included_indexes[-1]]
        latest = message.provider_timestamp or message.created_at
    return PreparedCommunicationInput(
        text="\n\n".join(block for _, block in selected),
        message_count_included=len(selected),
        message_count_available=len(messages),
        latest_message_timestamp_included=latest,
    )


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string"},
        },
        "required": ["summary"],
    }


SYSTEM_INSTRUCTIONS = """Summarize only the supplied communication records using the fewest words necessary. Prefer one sentence when one sentence is sufficient; use two or more sentences only when material factual context would otherwise be lost. Begin directly with what happened. Preserve important facts, and include commitments or actions only when explicitly stated. Do not editorialize or add interpretation. Do not infer significance, sentiment, collaboration, status, or next steps. Do not invent owners, deadlines, risks, open questions, decisions, outcomes, or status changes. Do not treat silence as agreement. Avoid introductory narration such as 'the communication records detail,' 'the exchange indicates,' or 'the discussion focused on.' If the record is insufficient, say so briefly. Return only the requested JSON field."""


class OpenAICommunicationSummaryGenerator:
    def __init__(self) -> None:
        if config.AI_SUMMARY_PROVIDER.lower() != "openai":
            raise CommunicationSummaryError("unsupported_provider", "The configured summary provider is not supported.")
        if not config.AI_SUMMARY_API_KEY or not config.AI_SUMMARY_MODEL:
            raise CommunicationSummaryError("missing_configuration", "AI communication summaries are not configured.")

    def generate_summary(self, input_data: PreparedCommunicationInput) -> CommunicationSummaryResult:
        from openai import OpenAI
        provider_started = perf_counter()
        kwargs: dict[str, Any] = {
            "api_key": config.AI_SUMMARY_API_KEY,
            "timeout": config.AI_SUMMARY_TIMEOUT_SECONDS,
            "max_retries": config.AI_SUMMARY_MAX_RETRIES,
        }
        if config.AI_SUMMARY_BASE_URL:
            kwargs["base_url"] = config.AI_SUMMARY_BASE_URL
        client = OpenAI(**kwargs)
        try:
            request_started = perf_counter()
            response = client.responses.create(
                model=config.AI_SUMMARY_MODEL,
                instructions=SYSTEM_INSTRUCTIONS,
                input=input_data.text,
                text={"format": {"type": "json_schema", "name": "communication_summary", "strict": True, "schema": _schema()}},
                max_output_tokens=config.AI_SUMMARY_MAX_OUTPUT_TOKENS,
                temperature=config.AI_SUMMARY_TEMPERATURE,
            )
            openai_request_ms = (perf_counter() - request_started) * 1000
            parse_started = perf_counter()
            raw = response.output_text or ""
            parsed = json.loads(raw)
            if not isinstance(parsed, dict) or not isinstance(parsed.get("summary"), str) or not parsed["summary"].strip():
                raise ValueError("invalid structured response")
            response_parse_ms = (perf_counter() - parse_started) * 1000
        except CommunicationSummaryError:
            raise
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            raise CommunicationSummaryError("invalid_response", "The AI provider returned an invalid summary.") from exc
        except Exception as exc:
            name = type(exc).__name__.lower()
            code = "rate_limit" if "ratelimit" in name else "timeout" if "timeout" in name else "provider_error"
            logger.warning(
                "communication_summary_provider_timing %s",
                json.dumps({
                    "event": "communication_summary_provider_timing",
                    "outcome": "failed",
                    "provider": "openai",
                    "model": config.AI_SUMMARY_MODEL,
                    "openai_api_request_ms": round((perf_counter() - request_started) * 1000, 2),
                    "configured_max_retries": config.AI_SUMMARY_MAX_RETRIES,
                    "error_type": code,
                }, sort_keys=True),
            )
            raise CommunicationSummaryError(code, "The AI provider could not generate a summary.") from exc
        usage_obj = getattr(response, "usage", None)
        usage = {key: getattr(usage_obj, key, None) for key in ("input_tokens", "output_tokens", "total_tokens")} if usage_obj else {}
        return CommunicationSummaryResult(
            current_status=parsed["summary"].strip(),
            key_updates=[], open_questions=[], next_action="", waiting_on="unclear",
            provider="openai", model=config.AI_SUMMARY_MODEL, usage=usage,
            timings_ms={
                "openai_api_request": round(openai_request_ms, 2),
                "openai_response_parse": round(response_parse_ms, 2),
                "provider_total": round((perf_counter() - provider_started) * 1000, 2),
            },
        )


def summary_is_stale(db: Session, summary: OpportunityCommunicationSummary) -> bool:
    latest = (
        db.query(OpportunityCommunicationMessage)
        .filter(
            OpportunityCommunicationMessage.workspace_id == summary.workspace_id,
            OpportunityCommunicationMessage.opportunity_id == summary.opportunity_id,
        )
        .order_by(func.coalesce(OpportunityCommunicationMessage.provider_timestamp, OpportunityCommunicationMessage.created_at).desc(), OpportunityCommunicationMessage.id.desc())
        .first()
    )
    if not latest or not summary.latest_message_timestamp_included:
        return False
    latest_at = latest.provider_timestamp or latest.created_at
    return bool(latest_at and latest_at > summary.latest_message_timestamp_included)


def generate_and_save_summary(db: Session, *, workspace: Workspace, opportunity: Opportunity, user: User, generator: CommunicationSummaryGenerator | None = None) -> OpportunityCommunicationSummary:
    started = datetime.now(timezone.utc)
    total_started = perf_counter()
    timings: dict[str, float | int | str | bool] = {
        "microsoft_graph_authentication_ms": 0.0,
        "attachment_retrieval_ms": 0.0,
        "microsoft_graph_used": False,
        "attachments_used": False,
        "openai_request_count": 0,
    }
    phase_started = perf_counter()
    messages = select_messages(db, workspace_id=workspace.id, opportunity_id=opportunity.id)
    timings["message_retrieval_ms"] = round((perf_counter() - phase_started) * 1000, 2)
    phase_started = perf_counter()
    existing = db.query(OpportunityCommunicationSummary).filter_by(workspace_id=workspace.id, opportunity_id=opportunity.id).one_or_none()
    timings["summary_lookup_ms"] = round((perf_counter() - phase_started) * 1000, 2)
    if not messages:
        raise CommunicationSummaryError("no_messages", "There are no communication messages to summarize.")
    phase_started = perf_counter()
    prepared = prepare_communication_input(messages, max_chars=config.AI_SUMMARY_MAX_INPUT_CHARS)
    timings["prompt_construction_ms"] = round((perf_counter() - phase_started) * 1000, 2)
    timings["input_chars"] = len(prepared.text)
    timings["messages_available"] = prepared.message_count_available
    timings["messages_included"] = prepared.message_count_included
    try:
        phase_started = perf_counter()
        timings["openai_request_count"] = 1
        result = (generator or OpenAICommunicationSummaryGenerator()).generate_summary(prepared)
        timings["model_generation_total_ms"] = round((perf_counter() - phase_started) * 1000, 2)
        if result.timings_ms:
            timings.update(result.timings_ms)
    except CommunicationSummaryError as exc:
        timings["model_generation_total_ms"] = round((perf_counter() - phase_started) * 1000, 2)
        row = existing or OpportunityCommunicationSummary(workspace_id=workspace.id, organization_id=opportunity.organization_id, opportunity_id=opportunity.id, status="failed")
        row.last_error = exc.code
        if not existing:
            db.add(row)
        save_started = perf_counter()
        db.commit()
        timings["database_save_ms"] = round((perf_counter() - save_started) * 1000, 2)
        timings["total_service_ms"] = round((perf_counter() - total_started) * 1000, 2)
        logger.warning("communication_summary_timing %s", json.dumps({"event": "communication_summary_timing", "outcome": "failed", "workspace_id": workspace.id, "opportunity_id": opportunity.id, "provider": config.AI_SUMMARY_PROVIDER, "error_type": exc.code, **timings}, sort_keys=True))
        logger.warning("Communication summary failed workspace_id=%s opportunity_id=%s message_count=%s provider=%s duration_ms=%s error_type=%s", workspace.id, opportunity.id, len(messages), config.AI_SUMMARY_PROVIDER, int((datetime.now(timezone.utc)-started).total_seconds()*1000), exc.code)
        raise
    row = existing or OpportunityCommunicationSummary(workspace_id=workspace.id, organization_id=opportunity.organization_id, opportunity_id=opportunity.id)
    row.status = "ready"
    row.current_status = result.current_status
    row.key_updates_json = result.key_updates
    row.open_questions_json = result.open_questions
    row.next_action = result.next_action
    row.waiting_on = result.waiting_on
    row.provider = result.provider
    row.model = result.model
    row.message_count_included = prepared.message_count_included
    row.message_count_available = prepared.message_count_available
    row.latest_message_timestamp_included = prepared.latest_message_timestamp_included
    row.generated_at = datetime.now(timezone.utc)
    row.generated_by_user_id = user.id
    row.last_error = None
    if not existing:
        db.add(row)
    save_started = perf_counter()
    db.commit()
    timings["database_save_ms"] = round((perf_counter() - save_started) * 1000, 2)
    timings["total_service_ms"] = round((perf_counter() - total_started) * 1000, 2)
    logger.info("communication_summary_timing %s", json.dumps({"event": "communication_summary_timing", "outcome": "ready", "workspace_id": workspace.id, "opportunity_id": opportunity.id, "provider": result.provider, "model": result.model, **timings}, sort_keys=True))
    logger.info("Communication summary generated workspace_id=%s opportunity_id=%s message_count=%s provider=%s model=%s duration_ms=%s usage=%s", workspace.id, opportunity.id, len(messages), result.provider, result.model, int((datetime.now(timezone.utc)-started).total_seconds()*1000), result.usage)
    return row


def csrf_token(user_id: int, opportunity_id: int) -> str:
    return URLSafeTimedSerializer(config.SECRET_KEY, salt="communication-summary").dumps({"user_id": user_id, "opportunity_id": opportunity_id})


def validate_csrf_token(token: str, user_id: int, opportunity_id: int) -> bool:
    try:
        data = URLSafeTimedSerializer(config.SECRET_KEY, salt="communication-summary").loads(token, max_age=3600)
    except BadSignature:
        return False
    return data == {"user_id": user_id, "opportunity_id": opportunity_id}
