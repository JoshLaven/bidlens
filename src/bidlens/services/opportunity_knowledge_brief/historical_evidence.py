"""Deterministic material-history evidence for GUTS."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import re
from typing import Any

from sqlalchemy.orm import Session

from ... import config
from ...models import Opportunity, OpportunityHistoryEvent, OpportunityUpdateEvent
from .contracts import EvidenceCollectionResult, EvidenceSource
from .organizational_evidence import normalize_evidence_text


UPDATE_FIELDS = {
    "response_deadline": "Response deadline", "source_stage": "Source stage",
    "solicitation_number": "Solicitation number", "opportunity_type": "Opportunity type",
    "set_aside": "Set-aside", "eligibility": "Eligibility", "title": "Title", "agency": "Agency", "naics": "NAICS",
    "naics_title": "NAICS title",
}
HISTORY_TYPES = {"source_updated", "grants_synopsis_version", "grants_forecast_version"}
GRANTS_KEYS = (
    "source_version_key", "history_type", "version", "version_name", "updated_date",
    "modification_description", "modified_fields", "source_revision",
    "forecast_to_active", "official_status_transition",
)


class HistoricalEvidenceScopeError(ValueError):
    pass


def _canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _material_change(field: str, before: Any, after: Any) -> bool:
    left = normalize_evidence_text(str(before) if before is not None else "")
    right = normalize_evidence_text(str(after) if after is not None else "")
    if not left and not right:
        return False
    if left.casefold() == right.casefold():
        return False
    if field in {"title", "agency", "naics", "naics_title"}:
        compact_left = re.sub(r"[^a-z0-9]", "", left.casefold())
        compact_right = re.sub(r"[^a-z0-9]", "", right.casefold())
        if compact_left == compact_right:
            return False
    return True


class HistoricalEvidenceCollector:
    def __init__(
        self, db: Session, *, maximum_events: int = config.GUTS_MAX_HISTORY_EVENTS,
        maximum_total_characters: int = config.GUTS_MAX_TOTAL_HISTORY_CHARS,
    ):
        if maximum_events <= 0 or maximum_total_characters <= 0:
            raise ValueError("History collector limits must be positive.")
        self.db = db
        self.maximum_events = maximum_events
        self.maximum_total_characters = maximum_total_characters

    def collect(self, *, opportunity_id: int, organization_id: int) -> EvidenceCollectionResult:
        opportunity = self.db.get(Opportunity, opportunity_id)
        if opportunity is None or opportunity.organization_id != organization_id:
            raise HistoricalEvidenceScopeError("Opportunity is outside the requested organization scope.")
        updates = self.db.query(OpportunityUpdateEvent).filter(
            OpportunityUpdateEvent.organization_id == organization_id,
            OpportunityUpdateEvent.opportunity_id == opportunity_id,
        ).order_by(OpportunityUpdateEvent.detected_at.asc(), OpportunityUpdateEvent.id.asc()).all()
        history = self.db.query(OpportunityHistoryEvent).filter(
            OpportunityHistoryEvent.organization_id == organization_id,
            OpportunityHistoryEvent.opportunity_id == opportunity_id,
        ).order_by(OpportunityHistoryEvent.occurred_at.asc(), OpportunityHistoryEvent.id.asc()).all()
        omitted = Counter()
        sources: list[EvidenceSource] = []
        for event in updates:
            changes = event.changed_fields if isinstance(event.changed_fields, dict) else {}
            for field in sorted(changes):
                values = changes.get(field) if isinstance(changes.get(field), dict) else {}
                if field not in UPDATE_FIELDS:
                    omitted["update_field_not_allowed"] += 1
                    continue
                before, after = values.get("before"), values.get("after")
                if not _material_change(field, before, after):
                    omitted["non_material_change"] += 1
                    continue
                structured = {"field": field, "before": before, "after": after}
                text = _canonical(structured)
                sources.append(EvidenceSource(
                    source_id=f"opportunity_update:{event.id}:{field}",
                    source_class="historical_context", source_type="field_change",
                    authority="historical_record",
                    citation_label=f"{event.source or 'Source'} update, {event.detected_at:%b %d, %Y}",
                    text=text, occurred_at=event.detected_at, content_hash=_digest(text),
                    title=UPDATE_FIELDS[field], internal_model_name="OpportunityUpdateEvent",
                    internal_record_id=event.id, selected_character_count=len(text),
                    original_character_count=len(text),
                    provenance={"organization_id": organization_id, "opportunity_id": opportunity_id, "source": event.source},
                    structured_facts={"field_name": field, "before": before, "after": after},
                ))
        for event in history:
            if event.event_type not in HISTORY_TYPES:
                omitted["history_type_not_allowed"] += 1
                continue
            data = event.event_data if isinstance(event.event_data, dict) else {}
            if event.event_type == "source_updated":
                # OpportunityUpdateEvent is the canonical, structured representation.
                omitted["duplicate_source_update_history"] += 1
                continue
            structured = {key: data[key] for key in GRANTS_KEYS if data.get(key) not in (None, "", [], {})}
            if not structured:
                omitted["empty_allowed_history"] += 1
                continue
            if "modification_description" in structured:
                structured["modification_description"] = normalize_evidence_text(structured["modification_description"])
            text = _canonical(structured)
            sources.append(EvidenceSource(
                source_id=f"opportunity_history:{event.id}", source_class="historical_context",
                source_type=event.event_type, authority="historical_record",
                citation_label=f"Grants.gov update, {event.occurred_at:%b %d, %Y}",
                text=text, occurred_at=event.occurred_at, content_hash=_digest(text),
                title=structured.get("version_name") or "Grants.gov version",
                internal_model_name="OpportunityHistoryEvent", internal_record_id=event.id,
                selected_character_count=len(text), original_character_count=len(text),
                provenance={"organization_id": organization_id, "opportunity_id": opportunity_id, "source": event.source},
                structured_facts=structured,
            ))
        sources.sort(key=lambda source: (source.occurred_at, source.source_id))
        available = len(updates) + len(history)
        # Material transitions and deadline changes first, then newest records.
        priority = sorted(range(len(sources)), key=lambda index: (
            0 if sources[index].structured_facts.get("field_name") in {"source_stage", "response_deadline"} else 1,
            -(sources[index].occurred_at.timestamp() if sources[index].occurred_at else 0),
            sources[index].source_id,
        ))
        selected_indices: list[int] = []
        used = 0
        for index in priority:
            source = sources[index]
            if len(selected_indices) >= self.maximum_events:
                break
            if used + len(source.text) <= self.maximum_total_characters:
                selected_indices.append(index); used += len(source.text)
        selected_set = set(selected_indices)
        for index in range(len(sources)):
            if index not in selected_set:
                omitted["history_limit"] += 1
        selected = tuple(sources[index] for index in sorted(selected_indices))
        excluded = available - len(selected)
        # One update row can create several field sources; account at source granularity.
        available_sources = len(sources) + sum(omitted.values())
        excluded = available_sources - len(selected)
        return EvidenceCollectionResult(
            evidence=selected, available_count=available_sources, selected_count=len(selected),
            excluded_count=excluded, truncated=bool(excluded),
            omitted_reason_counts=dict(sorted(omitted.items())),
            latest_source_at=max((source.occurred_at for source in selected), default=None),
            total_selected_characters=sum(len(source.text) for source in selected),
        )
