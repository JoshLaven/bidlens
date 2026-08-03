from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from time import perf_counter
from typing import Any, Protocol

from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session
from sqlalchemy import func

from .. import config
from ..models import Opportunity, OpportunityCommunicationMessage, OpportunityCommunicationSummary, User, Workspace
from .communication_content import clean_message_body
from .opportunity_conversations import deduplicate_communication_messages
from .organizational_evidence import (
    StoredCommunicationEvidenceCollector, StoredNoteEvidenceCollector,
    combine_team_summary_evidence,
)
from .organizational_evidence_contracts import (
    OrganizationalEvidenceSelectionPolicy, TeamSummaryEvidenceBundle,
    TEAM_SUMMARY_INPUT_CONTRACT_VERSION,
)

logger = logging.getLogger(__name__)
WAITING_ON_VALUES = {"our_team", "external_party", "both", "nobody", "unclear"}


class CommunicationSummaryError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PreparedCommunicationInput:
    text: str
    message_count_included: int
    message_count_available: int
    latest_message_timestamp_included: datetime | None


@dataclass(frozen=True)
class PreparedTeamSummaryInput:
    text: str
    evidence: TeamSummaryEvidenceBundle
    message_count_included: int
    message_count_available: int
    note_count_included: int
    note_count_available: int
    latest_message_timestamp_included: datetime | None
    latest_note_timestamp_included: datetime | None


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
    def generate_summary(self, input_data: PreparedCommunicationInput | PreparedTeamSummaryInput) -> CommunicationSummaryResult: ...


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


TEAM_SUMMARY_SYSTEM_INSTRUCTIONS = """Create one concise executive narrative using only the supplied organizational communications and notes. Answer: What has our team communicated or recorded about this opportunity? Integrate the sources rather than summarizing emails and notes separately. Preserve substantive assessments, concerns, recommendations, partner ideas, routing, stakeholder involvement, internal decisions, planning, meaningful coordination, prior relevant experience, and substantive questions. Preserve actor attribution whenever a person expressed a view, recommendation, plan, concern, or purported official fact. For example, write 'Josh noted that the deadline had moved,' never 'The deadline moved.' Do not convert an individual's view into team consensus or a plan into a completed action. Do not restate solicitation or opportunity facts as objective truth. Do not infer significance, status, next steps, owners, deadlines, risks, decisions, or consensus. Prefer one sentence when sufficient and add sentences only for material, distinct organizational context. Begin directly with what the people involved communicated or recorded; do not editorialize or narrate the summarization process. If the evidence is insufficient, say so briefly. Return only the requested JSON field."""

# Backwards-compatible public name used by existing integrations and tests.
SYSTEM_INSTRUCTIONS = TEAM_SUMMARY_SYSTEM_INSTRUCTIONS


def _selection_policies() -> tuple[OrganizationalEvidenceSelectionPolicy, OrganizationalEvidenceSelectionPolicy]:
    return (
        OrganizationalEvidenceSelectionPolicy(
            maximum_count=config.TEAM_SUMMARY_MAX_MESSAGES,
            maximum_item_characters=config.TEAM_SUMMARY_MAX_MESSAGE_CHARS,
            maximum_total_characters=config.TEAM_SUMMARY_MAX_TOTAL_MESSAGE_CHARS,
        ),
        OrganizationalEvidenceSelectionPolicy(
            maximum_count=config.TEAM_SUMMARY_MAX_NOTES,
            maximum_item_characters=config.TEAM_SUMMARY_MAX_NOTE_CHARS,
            maximum_total_characters=config.TEAM_SUMMARY_MAX_TOTAL_NOTE_CHARS,
        ),
    )


def collect_team_summary_evidence(
    db: Session, *, workspace: Workspace, opportunity: Opportunity,
) -> TeamSummaryEvidenceBundle:
    communication_policy, note_policy = _selection_policies()
    scope = {
        "opportunity_id": opportunity.id,
        "organization_id": opportunity.organization_id,
        "workspace_id": workspace.id,
    }
    communications = StoredCommunicationEvidenceCollector(
        db, policy=communication_policy,
    ).collect(**scope)
    notes = StoredNoteEvidenceCollector(db, policy=note_policy).collect(**scope)
    return combine_team_summary_evidence(
        **scope, communications=communications, notes=notes,
        communication_policy=communication_policy, note_policy=note_policy,
    )


def prepare_team_summary_input(bundle: TeamSummaryEvidenceBundle) -> PreparedTeamSummaryInput:
    blocks: list[str] = []
    for item in bundle.items:
        author = item.author.display_name or "Unknown contributor"
        lines = [
            f"Source: {item.source_id}",
            f"Type: {item.source_type}",
            f"Date: {item.occurred_at.isoformat() if item.occurred_at else 'Unknown'}",
            f"Author: {author}",
        ]
        if item.source_type == "communication":
            lines.extend((
                f"Direction: {item.direction or 'unknown'}",
                f"Recipients: {', '.join(item.recipients) or 'Unknown'}",
                f"Subject: {item.title or '(No subject)'}",
            ))
        lines.append(f"Content: {item.text}")
        blocks.append("\n".join(lines))
    latest = lambda source_type: max(
        (item.occurred_at for item in bundle.items if item.source_type == source_type and item.occurred_at),
        default=None,
    )
    return PreparedTeamSummaryInput(
        text="\n\n".join(blocks), evidence=bundle,
        message_count_included=bundle.selected_counts.get("communication", 0),
        message_count_available=bundle.available_counts.get("communication", 0),
        note_count_included=bundle.selected_counts.get("note", 0),
        note_count_available=bundle.available_counts.get("note", 0),
        latest_message_timestamp_included=latest("communication"),
        latest_note_timestamp_included=latest("note"),
    )


class OpenAICommunicationSummaryGenerator:
    def __init__(self) -> None:
        if config.AI_SUMMARY_PROVIDER.lower() != "openai":
            raise CommunicationSummaryError("unsupported_provider", "The configured summary provider is not supported.")
        if not config.AI_SUMMARY_API_KEY or not config.AI_SUMMARY_MODEL:
            raise CommunicationSummaryError("missing_configuration", "AI communication summaries are not configured.")

    def generate_summary(self, input_data: PreparedCommunicationInput | PreparedTeamSummaryInput) -> CommunicationSummaryResult:
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
    if (
        not summary.evidence_fingerprint
        or summary.input_contract_version != TEAM_SUMMARY_INPUT_CONTRACT_VERSION
        or summary.prompt_version != config.TEAM_SUMMARY_PROMPT_VERSION
    ):
        return True
    workspace = db.get(Workspace, summary.workspace_id)
    opportunity = db.get(Opportunity, summary.opportunity_id)
    if workspace is None or opportunity is None:
        return True
    current = collect_team_summary_evidence(db, workspace=workspace, opportunity=opportunity)
    return current.evidence_fingerprint != summary.evidence_fingerprint


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
    bundle = collect_team_summary_evidence(db, workspace=workspace, opportunity=opportunity)
    timings["message_retrieval_ms"] = round((perf_counter() - phase_started) * 1000, 2)
    phase_started = perf_counter()
    existing = db.query(OpportunityCommunicationSummary).filter_by(workspace_id=workspace.id, opportunity_id=opportunity.id).one_or_none()
    timings["summary_lookup_ms"] = round((perf_counter() - phase_started) * 1000, 2)
    if not bundle.items:
        raise CommunicationSummaryError("no_evidence", "There are no communications or notes to summarize.")
    phase_started = perf_counter()
    prepared = prepare_team_summary_input(bundle)
    timings["prompt_construction_ms"] = round((perf_counter() - phase_started) * 1000, 2)
    timings["input_chars"] = len(prepared.text)
    timings["messages_available"] = prepared.message_count_available
    timings["messages_included"] = prepared.message_count_included
    timings["notes_available"] = prepared.note_count_available
    timings["notes_included"] = prepared.note_count_included
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
        logger.warning("Team summary failed workspace_id=%s opportunity_id=%s evidence_count=%s provider=%s duration_ms=%s error_type=%s", workspace.id, opportunity.id, len(bundle.items), config.AI_SUMMARY_PROVIDER, int((datetime.now(timezone.utc)-started).total_seconds()*1000), exc.code)
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
    row.note_count_included = prepared.note_count_included
    row.note_count_available = prepared.note_count_available
    row.latest_note_timestamp_included = prepared.latest_note_timestamp_included
    row.evidence_fingerprint = bundle.evidence_fingerprint
    row.input_contract_version = bundle.contract_version
    row.prompt_version = config.TEAM_SUMMARY_PROMPT_VERSION
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
    logger.info("Team summary generated workspace_id=%s opportunity_id=%s evidence_count=%s provider=%s model=%s duration_ms=%s usage=%s", workspace.id, opportunity.id, len(bundle.items), result.provider, result.model, int((datetime.now(timezone.utc)-started).total_seconds()*1000), result.usage)
    return row


def csrf_token(user_id: int, opportunity_id: int) -> str:
    return URLSafeTimedSerializer(config.SECRET_KEY, salt="communication-summary").dumps({"user_id": user_id, "opportunity_id": opportunity_id})


def validate_csrf_token(token: str, user_id: int, opportunity_id: int) -> bool:
    try:
        data = URLSafeTimedSerializer(config.SECRET_KEY, salt="communication-summary").loads(token, max_age=3600)
    except BadSignature:
        return False
    return data == {"user_id": user_id, "opportunity_id": opportunity_id}
