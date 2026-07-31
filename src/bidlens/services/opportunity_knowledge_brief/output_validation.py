"""Strict deterministic validation for GUTS provider output."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Iterable

from ... import config
from .contracts import (
    EvidenceSource, GUTSManifest, ModelBriefingOutput, ModelOutputStatement,
    ModelSection, ModelStatement, ValidatedBriefingOutput, ValidatedModelBriefing,
)


SECTION_ORDER = (
    "current_state", "official_updates", "organizational_knowledge",
    "important_history", "uncertainties",
)
SECTION_TITLES = {
    "current_state": "Current State", "official_updates": "Official Updates",
    "organizational_knowledge": "Organizational Knowledge",
    "important_history": "Important History", "uncertainties": "Uncertainties",
}
PROHIBITED_PATTERNS = (
    (re.compile(r"\b(?:you|the team|we) should\b", re.I), "recommendation"),
    (re.compile(r"\b(?:recommend|recommended|next steps?)\b", re.I), "recommendation"),
    (re.compile(r"\b(?:likely|probably|presumably|i think|i believe|as an ai)\b", re.I), "speculation"),
    (re.compile(r"\baccording to the prompt\b", re.I), "prompt_reference"),
    (re.compile(r"\b(?:should|recommend(?:ed)?)\s+(?:not\s+)?bid\b", re.I), "bid_advice"),
    (re.compile(r"(?:^|\n)\s*#{1,6}\s+", re.M), "markdown"),
    (re.compile(r"(?:^|\n)\s*[-*]\s+|`|\[[^\]]+\]\([^\)]+\)", re.M), "markdown"),
    (re.compile(r"<\/?[a-z][^>]*>", re.I), "html"),
    (re.compile(r"\[[^\]]*(?:source|citation)[^\]]*\]", re.I), "citation_markup"),
    (re.compile(r"\[(?:\d+|[A-Za-z][^\]]*)\]"), "citation_markup"),
    (re.compile(r"\b(?:current_state|source_material|external_document|opportunity_note|communication|opportunity_update|opportunity_history):", re.I), "source_id_in_prose"),
)
ATTRIBUTION_CUES = re.compile(
    r"\b(?:said|noted|reported|plans?|planned|proposed|intends?|expects?|believes?|"
    r"identified|raised|asked|confirmed|according to|observed|described|expressed)\b", re.I,
)
UNCERTAINTY_CUES = re.compile(r"\b(?:uncertain|unknown|unresolved|unclear|not provided|not identified|question)\b|\?", re.I)
DATE_PATTERN = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b|\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},\s+\d{4}\b",
    re.I,
)


class GUTSValidationError(RuntimeError):
    stage = "output_validation"
    retryable = True

    def __init__(self, safe_category: str, safe_message: str, feedback: str):
        super().__init__(safe_message)
        self.safe_category = safe_category
        self.safe_message = safe_message
        self.feedback = feedback


@dataclass(frozen=True)
class SourceMetadata:
    source_id: str
    source_class: str
    source_type: str
    searchable: str
    source: EvidenceSource | None = None


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _date_variants(value: str) -> set[str]:
    variants = {value.casefold()}
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y"):
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        rendered = {
            parsed.strftime("%Y-%m-%d").casefold(),
            parsed.strftime("%B %d, %Y").casefold(),
            parsed.strftime("%b %d, %Y").casefold(),
        }
        variants.update(rendered)
        variants.update(item.replace(" 0", " ") for item in rendered)
    return variants


def _current_sources(manifest: GUTSManifest) -> dict[str, SourceMetadata]:
    state = manifest.current_state
    mapping: dict[str, SourceMetadata] = {}
    for name in (
        "title", "client", "description", "response_deadline", "posted_date", "solicitation_number",
        "opportunity_type", "source_stage", "source", "source_record_id", "source_url", "sam_url",
        "bidlens_id", "sam_notice_id", "naics", "naics_title", "set_aside",
    ):
        field = getattr(state, name)
        mapping[field.source_id] = SourceMetadata(
            source_id=field.source_id, source_class="current_state", source_type=name,
            searchable=json.dumps({"field": name, "value": field.value}, ensure_ascii=False, sort_keys=True),
        )
    if state.outcome:
        source_id = f"current_state:opportunity:{state.opportunity_id}:organization_outcome"
        mapping[source_id] = SourceMetadata(
            source_id=source_id, source_class="current_state", source_type="organization_outcome",
            searchable=state.outcome.canonical_json(),
        )
    return mapping


def _source_map(manifest: GUTSManifest) -> dict[str, SourceMetadata]:
    mapping = _current_sources(manifest)
    for source in manifest.evidence.sources:
        mapping[source.source_id] = SourceMetadata(
            source_id=source.source_id, source_class=source.source_class, source_type=source.source_type,
            searchable=" ".join((source.text, json.dumps(source.structured_facts, sort_keys=True, ensure_ascii=False), json.dumps(source.provenance, sort_keys=True, ensure_ascii=False), source.title or "")),
            source=source,
        )
    return mapping


class GUTSOutputValidator:
    def __init__(
        self, *, max_summary_statements: int = config.GUTS_MAX_SUMMARY_STATEMENTS,
        max_sections: int = config.GUTS_MAX_SECTIONS,
        max_statements_per_section: int = config.GUTS_MAX_STATEMENTS_PER_SECTION,
        max_statement_characters: int = config.GUTS_MAX_STATEMENT_CHARS,
        max_total_characters: int = config.GUTS_MAX_TOTAL_OUTPUT_CHARS,
    ):
        self.max_summary_statements = max_summary_statements
        self.max_sections = max_sections
        self.max_statements_per_section = max_statements_per_section
        self.max_statement_characters = max_statement_characters
        self.max_total_characters = max_total_characters

    def validate(self, output: ModelBriefingOutput, manifest: GUTSManifest) -> ValidatedBriefingOutput:
        if output.headline.statement_key != "headline":
            self._fail("model_schema_invalid", "The headline key was invalid.", "Use statement_key 'headline' for the headline.")
        if output.headline.importance != "high":
            self._fail("model_schema_invalid", "The headline importance was invalid.", "Use high importance for the headline.")
        if not output.summary_statements:
            self._fail("model_schema_invalid", "The summary was empty.", "Return at least one summary statement.")
        if len(output.summary_statements) > self.max_summary_statements or len(output.sections) > self.max_sections:
            self._fail("model_output_unsafe", "The briefing exceeded structural limits.", "Return fewer summary statements and sections.")
        if any(not section.statements for section in output.sections):
            self._fail("model_schema_invalid", "An empty section was returned.", "Omit empty sections.")
        if any(len(section.statements) > self.max_statements_per_section for section in output.sections):
            self._fail("model_output_unsafe", "A section contained too many statements.", "Return fewer statements per section.")
        section_types = [section.section_type for section in output.sections]
        if len(section_types) != len(set(section_types)):
            self._fail("model_schema_invalid", "A section type was duplicated.", "Return each section type at most once.")
        statements = [output.headline, *output.summary_statements, *(statement for section in output.sections for statement in section.statements)]
        keys = [statement.statement_key for statement in statements]
        if any(not key.strip() for key in keys):
            self._fail("model_schema_invalid", "A statement key was blank.", "Return a non-blank statement_key for every statement.")
        if len(keys) != len(set(keys)):
            self._fail("model_schema_invalid", "Statement keys were duplicated.", "Use a unique statement_key for every statement.")
        total_text = " ".join(statement.text for statement in statements)
        if len(total_text) > self.max_total_characters or len(total_text.split()) > 500:
            self._fail("model_output_unsafe", "The briefing exceeded the output length limit.", "Shorten the briefing to at most 500 words.")
        sources = _source_map(manifest)
        conflict_source_ids = {
            source_id for conflict in manifest.evidence.known_conflicts
            for source_id in (conflict.authoritative_source_id, conflict.conflicting_source_id)
        }
        normalized: dict[str, ModelStatement] = {}
        for position, statement in enumerate(statements):
            normalized[statement.statement_key] = self._validate_statement(
                statement, sources=sources, conflict_source_ids=conflict_source_ids,
                manifest=manifest, position=position,
            )
        for section in output.sections:
            self._validate_section(section.section_type, section.statements, sources)
        ordered_sections = sorted(output.sections, key=lambda section: SECTION_ORDER.index(section.section_type))
        sections = tuple(ModelSection(
            section_type=section.section_type, title=SECTION_TITLES[section.section_type], position=index,
            statements=tuple(normalized[statement.statement_key].model_copy(update={
                "placement_type": "section", "section_type": section.section_type, "position": statement_index,
            }) for statement_index, statement in enumerate(section.statements)),
        ) for index, section in enumerate(ordered_sections))
        return ValidatedBriefingOutput(
            output_schema_version=config.GUTS_OUTPUT_SCHEMA_VERSION,
            briefing=ValidatedModelBriefing(
                headline=normalized[output.headline.statement_key].model_copy(update={"placement_type": "headline", "position": 0}),
                summary=tuple(normalized[item.statement_key].model_copy(update={"placement_type": "summary", "position": index}) for index, item in enumerate(output.summary_statements)),
                sections=sections,
            ),
            validated_at=datetime.now(timezone.utc),
        )

    def _validate_statement(
        self, statement: ModelOutputStatement, *, sources: dict[str, SourceMetadata],
        conflict_source_ids: set[str], manifest: GUTSManifest, position: int,
    ) -> ModelStatement:
        text = _normalize_text(statement.text)
        if not text:
            self._fail("model_schema_invalid", "A statement was blank.", "Return non-blank statement text.")
        if len(text) > self.max_statement_characters:
            self._fail("model_output_unsafe", "A statement was too long.", "Shorten each statement and keep it atomic.")
        if len(statement.source_ids) != len(set(statement.source_ids)):
            self._fail("model_citation_invalid", "A citation was duplicated.", "Use each source ID at most once per statement.")
        unknown = [source_id for source_id in statement.source_ids if source_id not in sources]
        if unknown:
            self._fail("model_citation_invalid", "The response referenced unavailable sources.", "Use only source IDs present in the manifest.")
        for source_id in statement.source_ids:
            if source_id.casefold() in text.casefold():
                self._fail("model_output_unsafe", "A raw source ID appeared in prose.", "Keep source IDs only in source_ids arrays.")
        for pattern, reason in PROHIBITED_PATTERNS:
            if pattern.search(text):
                self._fail("model_output_unsafe", f"The response contained prohibited {reason} language.", "Remove recommendations, speculation, markup, and raw citation syntax.")
        if re.search(r"(?<![A-Z])[.!?]\s+[A-Z]", text) or ";" in text:
            self._fail("model_output_unsafe", "A statement contained multiple apparent claims.", "Return atomic statements containing one independently supportable idea.")
        cited = [sources[source_id] for source_id in statement.source_ids]
        classes = {source.source_class for source in cited}
        if statement.confidence == "supported" and not classes.intersection({"current_state", "official_evidence"}):
            self._fail("model_citation_invalid", "A supported statement lacked authoritative evidence.", "Label organizational claims attributed and cite their organizational sources.")
        if statement.confidence == "attributed":
            if "organizational_knowledge" not in classes:
                self._fail("model_citation_invalid", "An attributed statement lacked organizational evidence.", "Attributed statements must cite organizational knowledge.")
            if not ATTRIBUTION_CUES.search(text):
                self._fail("model_output_unsafe", "An attributed claim did not preserve attribution.", "Use wording such as reported, proposed, plans, or noted.")
        if statement.confidence == "uncertain":
            evidence_uncertain = any(
                UNCERTAINTY_CUES.search(source.searchable)
                or bool(source.source and source.source.structured_facts.get("uncertain"))
                for source in cited
            )
            if not evidence_uncertain and not set(statement.source_ids).intersection(conflict_source_ids):
                self._fail("model_citation_invalid", "An uncertainty lacked supporting evidence.", "Cite evidence that explicitly expresses the unresolved information.")
        if re.search(r"\b(?:because|therefore|as a result)\b", text, re.I) and not any(
            source.source and source.source.structured_facts.get("causal_statement") is True for source in cited
        ):
            self._fail("model_output_unsafe", "Unsupported causal language was used.", "State supported facts separately without inferring causality.")
        self._validate_dates_and_identifiers(text, statement.source_ids, sources, manifest)
        return ModelStatement(
            statement_key=statement.statement_key, placement_type="summary", section_type=None,
            position=position, text=text, importance=statement.importance,
            confidence=statement.confidence, source_ids=tuple(statement.source_ids),
        )

    def _validate_section(self, section_type: str, statements: Iterable[ModelOutputStatement], sources: dict[str, SourceMetadata]) -> None:
        for statement in statements:
            classes = {sources[source_id].source_class for source_id in statement.source_ids if source_id in sources}
            valid = {
                "current_state": bool(classes.intersection({"current_state", "official_evidence"})),
                "official_updates": bool(classes.intersection({"official_evidence", "historical_context"})),
                "organizational_knowledge": "organizational_knowledge" in classes,
                "important_history": "historical_context" in classes,
                "uncertainties": statement.confidence == "uncertain",
            }[section_type]
            if not valid:
                self._fail("model_citation_invalid", "A section did not match its cited evidence.", "Place statements only in sections compatible with their source classes.")
            if section_type == "uncertainties" and statement.confidence != "uncertain":
                self._fail("model_citation_invalid", "An uncertainty section used the wrong confidence.", "Use uncertain confidence for every uncertainties statement.")

    def _validate_dates_and_identifiers(self, text: str, source_ids: tuple[str, ...], sources: dict[str, SourceMetadata], manifest: GUTSManifest) -> None:
        cited_searchable = " ".join(sources[source_id].searchable for source_id in source_ids).casefold()
        for match in DATE_PATTERN.findall(text):
            if not any(variant in cited_searchable for variant in _date_variants(match)):
                self._fail("model_citation_invalid", "A date was not grounded by its citations.", "Cite the source containing each exact date.")
        deadline = manifest.current_state.response_deadline.value
        if deadline:
            deadline_variants = _date_variants(str(deadline))
            if any(variant in text.casefold() for variant in deadline_variants) and manifest.current_state.response_deadline.source_id not in source_ids:
                self._fail("model_citation_invalid", "The current deadline used the wrong citation.", "Cite the current response_deadline source for the operative deadline.")
        solicitation = manifest.current_state.solicitation_number.value
        if solicitation and str(solicitation).casefold() in text.casefold() and manifest.current_state.solicitation_number.source_id not in source_ids:
            self._fail("model_citation_invalid", "The solicitation number used the wrong citation.", "Cite the current solicitation_number source for the identifier.")

    @staticmethod
    def _fail(category: str, message: str, feedback: str):
        raise GUTSValidationError(category, message, feedback)
