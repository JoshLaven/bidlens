"""Normalize provider-owned output into canonical application-owned output."""

from __future__ import annotations

from .contracts import (
    ModelBriefingOutput,
    ModelOutputSection,
    ModelOutputStatement,
    ProviderBriefingOutputV2,
    ProviderOutputStatementV2,
)


def _with_key(statement: ProviderOutputStatementV2, statement_key: str) -> ModelOutputStatement:
    return ModelOutputStatement(statement_key=statement_key, **statement.model_dump())


def assign_v2_statement_keys(output: ProviderBriefingOutputV2) -> ModelBriefingOutput:
    """Assign stable keys from canonical placement and one-based position."""
    return ModelBriefingOutput(
        headline=_with_key(output.headline, "headline"),
        summary_statements=tuple(
            _with_key(statement, f"summary_{position}")
            for position, statement in enumerate(output.summary_statements, start=1)
        ),
        sections=tuple(
            ModelOutputSection(
                section_type=section.section_type,
                statements=tuple(
                    _with_key(statement, f"{section.section_type}_{position}")
                    for position, statement in enumerate(section.statements, start=1)
                ),
            )
            for section in output.sections
        ),
    )
