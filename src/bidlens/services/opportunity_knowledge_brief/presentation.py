"""Read-only presentation mapping for persisted Get Up to Speed output."""

from __future__ import annotations

from dataclasses import dataclass

from ...models import (
    OpportunityKnowledgeBriefGeneration,
    OpportunityKnowledgeBriefStatement,
)
from .constants import PlacementType, SectionType


@dataclass(frozen=True)
class GUTSCitationPresentation:
    source_id: str
    citation_label: str
    source_class: str
    source_type: str


@dataclass(frozen=True)
class GUTSStatementPresentation:
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
    risks_watch_items: tuple[GUTSStatementPresentation, ...] = ()
    suggested_next_steps: tuple[GUTSStatementPresentation, ...] = ()


def _present_statement(statement: OpportunityKnowledgeBriefStatement) -> GUTSStatementPresentation:
    return GUTSStatementPresentation(
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
            )
            for link in statement.source_links
        ),
    )


def build_guts_presentation(
    generation: OpportunityKnowledgeBriefGeneration | None,
) -> GUTSPresentation:
    """Group persisted canonical statements without merging or rewriting them."""
    if generation is None:
        return GUTSPresentation()

    overall: list[GUTSStatementPresentation] = []
    recent: list[GUTSStatementPresentation] = []
    internal: list[GUTSStatementPresentation] = []
    risks: list[GUTSStatementPresentation] = []
    history: list[GUTSStatementPresentation] = []

    placement_order = {
        PlacementType.HEADLINE: 0,
        PlacementType.SUMMARY: 1,
        PlacementType.SECTION: 2,
    }
    statements = sorted(
        generation.statements,
        key=lambda statement: (
            placement_order.get(statement.placement_type, 3),
            statement.position,
            statement.id or 0,
        ),
    )
    for statement in statements:
        presented = _present_statement(statement)
        if statement.placement_type in {PlacementType.HEADLINE, PlacementType.SUMMARY}:
            overall.append(presented)
        elif statement.section_type == SectionType.CURRENT_STATE:
            overall.append(presented)
        elif statement.section_type == SectionType.OFFICIAL_UPDATES:
            recent.append(presented)
        elif statement.section_type == SectionType.ORGANIZATIONAL_KNOWLEDGE:
            internal.append(presented)
        elif statement.section_type == SectionType.IMPORTANT_HISTORY:
            history.append(presented)
        elif statement.section_type == SectionType.UNCERTAINTIES:
            risks.append(presented)

    # Important history is secondary context for developments. If the canonical
    # output has no direct orientation statement, it also provides the status fallback.
    recent.extend(history)
    if not overall:
        overall.extend(history)

    return GUTSPresentation(
        overall_status=tuple(overall),
        recent_developments=tuple(recent),
        internal_activity=tuple(internal),
        risks_watch_items=tuple(risks),
        # The current canonical contract has no recommendation section. Keep the
        # presentation slot empty until one is added by the canonical output itself.
        suggested_next_steps=(),
    )
