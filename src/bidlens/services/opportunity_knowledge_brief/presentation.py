"""Read-only presentation mapping for persisted Get Up to Speed output."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ...models import (
    OpportunityKnowledgeBriefGeneration,
    OpportunityKnowledgeBriefStatement,
)
from .constants import Importance, PlacementType, SectionType


OVERALL_STATUS_LIMIT = 2
RECENT_DEVELOPMENTS_LIMIT = 3
INTERNAL_ACTIVITY_LIMIT = 3


@dataclass(frozen=True)
class GUTSCitationPresentation:
    source_id: str
    citation_label: str
    source_class: str
    source_type: str
    occurred_at: datetime | None = None
    effective_at: datetime | None = None
    updated_at_source: datetime | None = None


@dataclass(frozen=True)
class GUTSStatementPresentation:
    persisted_statement_id: int | None
    statement_key: str
    text: str
    confidence: str
    importance: str
    citations: tuple[GUTSCitationPresentation, ...]


@dataclass(frozen=True)
class GUTSPresentation:
    overall_status: tuple[GUTSStatementPresentation, ...] = ()
    recent_developments: tuple[GUTSStatementPresentation, ...] = ()
    internal_activity: tuple[GUTSStatementPresentation, ...] = ()


def _present_statement(statement: OpportunityKnowledgeBriefStatement) -> GUTSStatementPresentation:
    return GUTSStatementPresentation(
        persisted_statement_id=statement.id,
        statement_key=statement.statement_key,
        text=statement.text,
        confidence=statement.confidence,
        importance=statement.importance,
        citations=tuple(
            GUTSCitationPresentation(
                source_id=link.brief_source.source_id,
                citation_label=link.brief_source.citation_label,
                source_class=link.brief_source.source_class,
                source_type=link.brief_source.source_type,
                occurred_at=getattr(link.brief_source, "occurred_at", None),
                effective_at=getattr(link.brief_source, "effective_at", None),
                updated_at_source=getattr(link.brief_source, "updated_at_source", None),
            )
            for link in statement.source_links
        ),
    )


def _canonical_key(statement: OpportunityKnowledgeBriefStatement) -> tuple[int, int, int]:
    placement_order = {
        PlacementType.HEADLINE: 0,
        PlacementType.SUMMARY: 1,
        PlacementType.SECTION: 2,
    }
    return (
        placement_order.get(statement.placement_type, 3),
        statement.position,
        statement.id or 0,
    )


def _current_state_fields(statement: OpportunityKnowledgeBriefStatement) -> frozenset[str]:
    prefix = "current_state:opportunity:"
    return frozenset(
        source_id.rsplit(":", 1)[-1]
        for source_id in (
            link.brief_source.source_id for link in statement.source_links
        )
        if source_id.startswith(prefix)
    )


def _field_rank(statement: OpportunityKnowledgeBriefStatement, priorities: tuple[str, ...]) -> int:
    fields = _current_state_fields(statement)
    return min((priorities.index(field) for field in fields if field in priorities), default=len(priorities))


def _select_overall(
    statements: list[OpportunityKnowledgeBriefStatement],
) -> tuple[GUTSStatementPresentation, ...]:
    candidates = [
        statement
        for statement in statements
        if statement.placement_type in {PlacementType.HEADLINE, PlacementType.SUMMARY}
        or statement.section_type == SectionType.CURRENT_STATE
    ]
    if not candidates:
        return ()

    importance_rank = lambda statement: 0 if statement.importance == Importance.HIGH else 1
    orientation_priorities = ("source_stage", "opportunity_type", "description", "client", "title")
    operative_priorities = ("response_deadline", "organization_outcome", "source_stage", "posted_date")

    orientation = sorted(
        candidates,
        key=lambda statement: (
            _current_state_fields(statement) == {"title"},
            _field_rank(statement, orientation_priorities),
            importance_rank(statement),
            _canonical_key(statement),
        ),
    )[0]
    selected = [orientation]
    used_fields = _current_state_fields(orientation)

    remaining = [statement for statement in candidates if statement is not orientation]
    if remaining:
        operative = sorted(
            remaining,
            key=lambda statement: (
                bool(_current_state_fields(statement) & used_fields),
                _field_rank(statement, operative_priorities),
                importance_rank(statement),
                _canonical_key(statement),
            ),
        )[0]
        selected.append(operative)

    return tuple(_present_statement(statement) for statement in selected[:OVERALL_STATUS_LIMIT])


def _source_timestamp(statement: OpportunityKnowledgeBriefStatement) -> float:
    timestamps = []
    for link in statement.source_links:
        source = link.brief_source
        for value in (
            getattr(source, "effective_at", None),
            getattr(source, "occurred_at", None),
            getattr(source, "updated_at_source", None),
        ):
            if value is not None:
                normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
                timestamps.append(normalized.timestamp())
    return max(timestamps, default=float("-inf"))


def build_guts_presentation(
    generation: OpportunityKnowledgeBriefGeneration | None,
) -> GUTSPresentation:
    """Select and group canonical statements without merging or rewriting them.

    Canonical ``official_updates`` currently does not carry a structured signal that
    distinguishes a true change from a static official fact. V1 therefore uses only
    ``important_history`` for Recent Developments instead of inferring from prose.
    """
    if generation is None:
        return GUTSPresentation()

    statements = sorted(generation.statements, key=_canonical_key)
    history = [
        statement for statement in statements
        if statement.placement_type == PlacementType.SECTION
        and statement.section_type == SectionType.IMPORTANT_HISTORY
    ]
    history.sort(
        key=lambda statement: (
            -_source_timestamp(statement),
            0 if statement.importance == Importance.HIGH else 1,
            _canonical_key(statement),
        )
    )
    internal = [
        statement for statement in statements
        if statement.placement_type == PlacementType.SECTION
        and statement.section_type == SectionType.ORGANIZATIONAL_KNOWLEDGE
    ]
    internal.sort(
        key=lambda statement: (
            0 if statement.importance == Importance.HIGH else 1,
            _canonical_key(statement),
        )
    )

    return GUTSPresentation(
        overall_status=_select_overall(statements),
        recent_developments=tuple(
            _present_statement(statement)
            for statement in history[:RECENT_DEVELOPMENTS_LIMIT]
        ),
        internal_activity=tuple(
            _present_statement(statement)
            for statement in internal[:INTERNAL_ACTIVITY_LIMIT]
        ),
    )
