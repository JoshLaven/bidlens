"""Cross-class deduplication, structured conflicts, and final GUTS selection."""

from __future__ import annotations

from collections import Counter, deque
import hashlib
import json
import re
from typing import Iterable

from .contracts import (
    CurrentOpportunityState, EvidenceCollectionResult, EvidenceSelection,
    EvidenceSelectionStatistics, EvidenceSource, FinalEvidenceSelection,
    KnownConflict, OfficialEvidenceCollectionResult,
)
from .organizational_evidence import normalize_evidence_text


PRECEDENCE = {"current_state": 0, "official_evidence": 1, "organizational_knowledge": 2, "historical_context": 3}
CONFLICT_FIELDS = {
    "response_deadline": "response_deadline", "solicitation_number": "solicitation_number",
    "agency": "client", "client": "client", "source_stage": "source_stage",
    "opportunity_type": "opportunity_type", "set_aside": "set_aside",
    "organization_outcome": "organization_outcome",
}


def _normalized_duplicate_text(source: EvidenceSource) -> str:
    return normalize_evidence_text(source.text).casefold()


class CrossClassDeduplicator:
    """Remove conservative exact duplicates while preserving meaningful attribution."""

    def deduplicate(self, sources: Iterable[EvidenceSource]) -> tuple[tuple[EvidenceSource, ...], dict[str, int]]:
        ordered = sorted(sources, key=lambda source: (
            PRECEDENCE[source.source_class], source.occurred_at.isoformat() if source.occurred_at else "", source.source_id,
        ))
        selected: list[EvidenceSource] = []
        by_content: dict[str, EvidenceSource] = {}
        omitted = Counter()
        for source in ordered:
            normalized_key = "text:" + hashlib.sha256(_normalized_duplicate_text(source).encode()).hexdigest()
            hash_key = "hash:" + source.content_hash if source.content_hash else normalized_key
            existing = by_content.get(hash_key) or by_content.get(normalized_key)
            if existing is None:
                by_content[hash_key] = source
                by_content[normalized_key] = source
                selected.append(source); continue
            independently_attributed = (
                source.source_class == "organizational_knowledge"
                and source.author is not None
                and (
                    existing.source_class != "organizational_knowledge"
                    or source.author.canonical_json() != (existing.author.canonical_json() if existing.author else "")
                )
            )
            if independently_attributed:
                selected.append(source)
            else:
                omitted["cross_class_exact_duplicate"] += 1
        selected.sort(key=lambda source: (
            PRECEDENCE[source.source_class], source.occurred_at.isoformat() if source.occurred_at else "", source.source_id,
        ))
        return tuple(selected), dict(omitted)


def _normalize_conflict_value(field: str, value):
    if value is None:
        return None
    text = normalize_evidence_text(str(value))
    if field == "response_deadline":
        return text[:10]
    if field in {"client", "agency", "solicitation_number", "source_stage", "opportunity_type", "set_aside"}:
        return re.sub(r"\s+", " ", text).casefold()
    return text


class ConflictDetector:
    def detect(
        self, *, current_state: CurrentOpportunityState, evidence: Iterable[EvidenceSource],
    ) -> tuple[KnownConflict, ...]:
        conflicts: list[KnownConflict] = []
        for source in evidence:
            facts = source.structured_facts
            raw_field = facts.get("field_name") or facts.get("field")
            field = CONFLICT_FIELDS.get(str(raw_field or ""))
            if not field:
                continue
            conflicting = (
                facts.get("before")
                if source.source_class == "historical_context" and source.source_type == "field_change"
                else facts.get("after", facts.get("value"))
            )
            if field == "organization_outcome":
                authoritative = current_state.outcome.outcome_type if current_state.outcome else None
                authoritative_source_id = f"current_state:opportunity:{current_state.opportunity_id}:organization_outcome"
            else:
                authoritative_field = getattr(current_state, field)
                authoritative = authoritative_field.value
                authoritative_source_id = authoritative_field.source_id
            if _normalize_conflict_value(field, authoritative) == _normalize_conflict_value(field, conflicting):
                continue
            if conflicting in (None, ""):
                continue
            resolution = "authoritative_current_wins"
            if source.source_class == "official_evidence" and facts.get("newer_official") is True:
                resolution = "newer_official_source_wins"
            elif source.source_class == "organizational_knowledge":
                resolution = "authoritative_current_wins"
            identity = f"{field}|{authoritative_source_id}|{source.source_id}|{authoritative}|{conflicting}|{resolution}"
            conflicts.append(KnownConflict(
                conflict_id=f"conflict:sha256:{hashlib.sha256(identity.encode()).hexdigest()}",
                field_name=field, authoritative_value=authoritative,
                authoritative_source_id=authoritative_source_id,
                conflicting_value=conflicting, conflicting_source_id=source.source_id,
                resolution=resolution, material=True, include_in_briefing=False,
            ))
        official_by_field: dict[str, list[EvidenceSource]] = {}
        for source in evidence:
            raw_field = source.structured_facts.get("field_name") or source.structured_facts.get("field")
            field = CONFLICT_FIELDS.get(str(raw_field or ""))
            if field and source.source_class == "official_evidence" and source.effective_at is not None:
                official_by_field.setdefault(field, []).append(source)
        for field, sources in official_by_field.items():
            ordered = sorted(sources, key=lambda source: (source.effective_at, source.source_id))
            newest = ordered[-1]
            newest_value = newest.structured_facts.get("value", newest.structured_facts.get("after"))
            for older in ordered[:-1]:
                older_value = older.structured_facts.get("value", older.structured_facts.get("after"))
                if _normalize_conflict_value(field, newest_value) == _normalize_conflict_value(field, older_value):
                    continue
                identity = f"{field}|{newest.source_id}|{older.source_id}|{newest_value}|{older_value}|newer_official_source_wins"
                conflicts.append(KnownConflict(
                    conflict_id=f"conflict:sha256:{hashlib.sha256(identity.encode()).hexdigest()}",
                    field_name=field, authoritative_value=newest_value,
                    authoritative_source_id=newest.source_id, conflicting_value=older_value,
                    conflicting_source_id=older.source_id, resolution="newer_official_source_wins",
                    material=True, include_in_briefing=False,
                ))
        return tuple(sorted(conflicts, key=lambda item: item.conflict_id))


class EvidenceSelector:
    def __init__(self, *, maximum_total_characters: int):
        if maximum_total_characters <= 0:
            raise ValueError("maximum_total_characters must be positive")
        self.maximum_total_characters = maximum_total_characters

    def select(
        self, *, current_state: CurrentOpportunityState,
        official: OfficialEvidenceCollectionResult, notes: EvidenceCollectionResult,
        communications: EvidenceCollectionResult, historical: EvidenceCollectionResult,
        known_conflicts: tuple[KnownConflict, ...] = (),
    ) -> FinalEvidenceSelection:
        all_sources = [*official.evidence, *notes.evidence, *communications.evidence, *historical.evidence]
        deduped, dedupe_omissions = CrossClassDeduplicator().deduplicate(all_sources)
        by_class = {
            "official_evidence": deque(source for source in deduped if source.source_class == "official_evidence"),
            "historical_context": deque(source for source in deduped if source.source_class == "historical_context"),
        }
        note_queue = deque(source for source in deduped if source.source_type == "note")
        message_queue = deque(source for source in deduped if source.source_type == "email")
        current_size = len(current_state.canonical_json())
        remaining = max(0, self.maximum_total_characters - current_size)
        selected: list[EvidenceSource] = []
        omitted = Counter(dedupe_omissions)
        for label, result in (
            ("official", official), ("notes", notes),
            ("communications", communications), ("history", historical),
        ):
            omitted.update({f"collector_{label}_{key}": count for key, count in result.omitted_reason_counts.items()})

        def consume(queue: deque[EvidenceSource], reason: str) -> None:
            nonlocal remaining
            while queue:
                source = queue.popleft()
                if len(source.text) <= remaining:
                    selected.append(source); remaining -= len(source.text)
                else:
                    omitted[reason] += 1

        reserved: list[EvidenceSource] = []
        reservation_size = 0
        for queue in (note_queue, message_queue):
            if queue and reservation_size + len(queue[0].text) <= remaining:
                item = queue.popleft(); reserved.append(item); reservation_size += len(item.text)
        remaining -= reservation_size
        consume(by_class["official_evidence"], "final_budget_official")
        selected.extend(reserved)
        # Round-robin guarantees both organizational source types an opportunity.
        while note_queue or message_queue:
            for queue in (note_queue, message_queue):
                if not queue:
                    continue
                source = queue.popleft()
                if len(source.text) <= remaining:
                    selected.append(source); remaining -= len(source.text)
                else:
                    omitted["final_budget_organizational"] += 1
        consume(by_class["historical_context"], "final_budget_historical")
        selected.sort(key=lambda source: (
            PRECEDENCE[source.source_class], source.occurred_at.isoformat() if source.occurred_at else "", source.source_id,
        ))
        selected_ids = {source.source_id for source in selected}
        unavailable = official.unavailable_sources
        original_chars = sum(source.original_character_count for source in selected)
        statistics = EvidenceSelectionStatistics(
            available_source_count=(official.available_count + notes.available_count + communications.available_count + historical.available_count),
            unavailable_source_count=len(unavailable),
            selected_character_count=sum(len(source.text) for source in selected),
            original_character_count=original_chars,
            truncated_source_count=sum(1 for source in selected if source.was_truncated),
        )
        latest = max((source.occurred_at for source in selected if source.occurred_at), default=None)
        reproducibility = "partially_reproducible" if any(not source.retained_by_bidlens for source in selected) else "fully_reproducible"
        return FinalEvidenceSelection(
            selection=EvidenceSelection(
                sources=tuple(selected), unavailable_sources=unavailable,
                known_conflicts=tuple(conflict for conflict in known_conflicts if (
                    (
                        conflict.authoritative_source_id.startswith("current_state:")
                        or conflict.authoritative_source_id in selected_ids
                    )
                    and conflict.conflicting_source_id in selected_ids
                )), statistics=statistics,
            ),
            latest_source_at=latest, reproducibility_status=reproducibility,
            omitted_reason_counts=dict(sorted(omitted.items())),
            input_truncated=bool(
                omitted or official.truncated or notes.truncated or communications.truncated
                or historical.truncated or unavailable
            ),
        )
