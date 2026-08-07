"""Strict deterministic validation for GUTS provider output."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import re
from typing import Iterable

from ... import config
from .contracts import (
    EvidenceSource, GUTSManifest, ModelBriefingOutput, ModelOutputStatement,
    ModelSection, ModelStatement, ValidatedBriefingOutput, ValidatedModelBriefing,
)
from .attribution import (
    actor_matches_author, author_identity_key, normalized_name_key, normalize_email,
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
SECTION_ALLOWED_SOURCE_CLASSES = {
    "current_state": ("current_state", "official_evidence"),
    "official_updates": ("official_evidence", "historical_context"),
    "organizational_knowledge": ("organizational_knowledge",),
    "important_history": ("historical_context",),
    "uncertainties": (
        "current_state", "official_evidence", "organizational_knowledge", "historical_context",
    ),
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
REPORTING_AND_PLANNING_ATTRIBUTION_VERBS = (
    r"suggest(?:s|ed)?|propos(?:e|es|ed)|not(?:e|es|ed)|sa(?:y|ys|id)|stat(?:e|es|ed)|"
    r"mention(?:s|ed)?|indicat(?:e|es|ed)|discuss(?:es|ed)?|consider(?:s|ed)?|"
    r"rais(?:e|es|ed)|identif(?:y|ies|ied)|recommend(?:s|ed)?|"
    r"report(?:s|ed)?|"
    r"express(?:es|ed)?|ask(?:s|ed)?|plan(?:s|ned)?|intend(?:s|ed)?"
)
EVALUATIVE_ATTRIBUTION_VERBS = (
    r"assess(?:es|ed)?|view(?:s|ed)?|consider(?:s|ed)?|believ(?:e|es|ed)|"
    r"observ(?:e|es|ed)|characteriz(?:e|es|ed)|describ(?:e|es|ed)|"
    r"conclud(?:e|es|ed)|expect(?:s|ed)?"
)
ATTRIBUTION_VERBS = (
    rf"(?:{REPORTING_AND_PLANNING_ATTRIBUTION_VERBS}|{EVALUATIVE_ATTRIBUTION_VERBS})"
)
ACTOR_ATTRIBUTION_MODIFIER = r"(?:initially\s+)?"
ACTOR_ATTRIBUTION = re.compile(
    rf"\b[A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*){{0,4}}\s+"
    rf"(?:has\s+|had\s+)?{ACTOR_ATTRIBUTION_MODIFIER}(?:{ATTRIBUTION_VERBS})\b",
)
TEAM_ATTRIBUTION = re.compile(rf"\bthe\s+team\s+(?:has\s+|had\s+)?(?:{ATTRIBUTION_VERBS})\b", re.I)
COORDINATED_ACTOR_ATTRIBUTION = re.compile(
    rf"\b[A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*){{0,3}}\s+and\s+"
    rf"[A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*){{0,3}}\s+(?:both\s+)?(?:{ATTRIBUTION_VERBS})\b"
)
INTERNAL_ATTRIBUTION = re.compile(
    r"\b(?:an?\s+)?internal\s+(?:note|discussion|record|communication)\s+"
    r"(?:indicates?|records?|notes?|identified|raised|discussed|reported)\b|"
    r"\bthe\s+available\s+(?:communication|internal)\s+records?\b|"
    r"\baccording\s+to\b",
    re.I,
)
OBJECTIVE_UPGRADE_PATTERNS = (
    re.compile(
        r"^(?:the\s+)?[\w&'’-]+(?:\s+[\w&'’-]+){0,5}\s+"
        r"(?:is|are|was|were)\s+(?:the\s+)?(?:selected\s+)?"
        r"(?:lead|subcontractor|responsible|confirmed|approved|assigned|confirmed\s+(?:risk|fact))\b",
        re.I,
    ),
    re.compile(
        r"^(?:the\s+)?[\w&'’-]+(?:\s+[\w&'’-]+){0,5}\s+"
        r"(?:contacted|completed|approved|selected|decided|assigned)\b",
        re.I,
    ),
    re.compile(
        r"^(?:the\s+)?(?:organization|team|company|staff)\s+has\s+"
        r"(?:prior\s+)?(?:experience|completed|confirmed|selected|approved)\b",
        re.I,
    ),
    re.compile(r"^(?:the\s+)?[\w&'’-]+(?:\s+[\w&'’-]+){0,5}\s+will\s+lead\b", re.I),
)
UNCERTAINTY_CUES = re.compile(r"\b(?:uncertain|unknown|unresolved|unclear|not provided|not identified|question)\b|\?", re.I)
DATE_PATTERN = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b|\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},\s+\d{4}\b",
    re.I,
)
logger = logging.getLogger(__name__)


class GUTSValidationError(RuntimeError):
    stage = "output_validation"
    retryable = True

    def __init__(
        self, safe_category: str, safe_message: str, feedback: str, *,
        invalid_source_ids: tuple[str, ...] = (), allowed_source_ids: tuple[str, ...] = (),
        statement_key: str | None = None, statement_placement: str | None = None,
        grounded_field: str | None = None, required_source_id: str | None = None,
        required_source_ids: tuple[str, ...] = (),
        cited_source_ids: tuple[str, ...] = (),
        statement_confidence: str | None = None, cited_source_kinds: tuple[str, ...] = (),
        rejected_statement_text: str | None = None, validator_rule: str | None = None,
        allowed_source_classes: tuple[str, ...] = (),
        required_source_classes: tuple[str, ...] = (),
        multiple_cited_actors: bool = False,
        statement_index: int | None = None, failure_subtype: str | None = None,
    ):
        super().__init__(safe_message)
        self.safe_category = safe_category
        self.safe_message = safe_message
        self.feedback = feedback
        self.invalid_source_ids = invalid_source_ids
        self.allowed_source_ids = allowed_source_ids
        self.statement_key = statement_key
        self.statement_placement = statement_placement
        self.grounded_field = grounded_field
        self.required_source_id = required_source_id
        self.required_source_ids = required_source_ids
        self.cited_source_ids = cited_source_ids
        self.statement_confidence = statement_confidence
        self.cited_source_kinds = cited_source_kinds
        self.rejected_statement_text = rejected_statement_text
        self.validator_rule = validator_rule
        self.allowed_source_classes = allowed_source_classes
        self.required_source_classes = required_source_classes
        self.multiple_cited_actors = multiple_cited_actors
        self.statement_index = statement_index
        self.failure_subtype = failure_subtype


class GUTSStatementKeyInvariantError(RuntimeError):
    """Application-owned V2 key assignment violated its uniqueness invariant."""

    stage = "output_validation"
    safe_category = "unexpected_error"
    safe_message = "The briefing could not be generated."

    def __init__(self, *, statement_key: str, statement_placement: str):
        super().__init__(self.safe_message)
        self.statement_key = statement_key
        self.statement_placement = statement_placement
        self.validator_rule = "deterministic_statement_key_uniqueness"


@dataclass(frozen=True)
class SourceMetadata:
    source_id: str
    source_class: str
    source_type: str
    searchable: str
    source: EvidenceSource | None = None


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def preserves_attribution(text: str) -> bool:
    """Recognize explicit grammatical attribution without judging source content."""
    return bool(
        ACTOR_ATTRIBUTION.search(text)
        or TEAM_ATTRIBUTION.search(text)
        or INTERNAL_ATTRIBUTION.search(text)
    )


def contains_objective_upgrade(text: str) -> bool:
    """Reject a small set of clearly objective organizational-only upgrades."""
    return any(pattern.search(text) for pattern in OBJECTIVE_UPGRADE_PATTERNS)


def _multiple_communication_actors(cited: list[SourceMetadata]) -> bool:
    actors: set[tuple[str, str]] = set()
    for metadata in cited:
        source = metadata.source
        if source is None or source.source_type != "email" or source.author is None:
            continue
        author = source.author
        if author.user_id is not None:
            actors.add(("user", str(author.user_id)))
        elif author.address:
            actors.add(("address", author.address.casefold()))
        elif author.display_name:
            actors.add(("name", author.display_name.casefold()))
    return len(actors) > 1


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
        "opportunity_type", "canonical_type", "source_stage", "source", "source_record_id", "source_url", "sam_url",
        "bidlens_id", "sam_notice_id", "naics", "naics_title", "set_aside",
    ):
        field = getattr(state, name)
        if field is None:
            continue
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
        output_schema_version: str = "guts-output-v1",
    ):
        self.max_summary_statements = max_summary_statements
        self.max_sections = max_sections
        self.max_statements_per_section = max_statements_per_section
        self.max_statement_characters = max_statement_characters
        self.max_total_characters = max_total_characters
        self.output_schema_version = output_schema_version

    def validate(self, output: ModelBriefingOutput, manifest: GUTSManifest) -> ValidatedBriefingOutput:
        if output.headline.statement_key != "headline":
            self._fail(
                "model_schema_invalid", "The headline key was invalid.",
                "The headline must use the exact reserved statement_key 'headline'; no alternate, shortened, numbered, or generated key is allowed.",
                statement_key=output.headline.statement_key,
                statement_placement="headline", validator_rule="headline_key",
            )
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
        statement_contexts = [
            (output.headline, "headline"),
            *((statement, "summary") for statement in output.summary_statements),
            *((statement, f"section:{section.section_type}") for section in output.sections for statement in section.statements),
        ]
        statements = [statement for statement, _placement in statement_contexts]
        keys = [statement.statement_key for statement in statements]
        if any(not key.strip() for key in keys):
            self._fail("model_schema_invalid", "A statement key was blank.", "Return a non-blank statement_key for every statement.")
        if len(keys) != len(set(keys)):
            if self.output_schema_version == "guts-output-v2":
                duplicate_index = next(index for index, key in enumerate(keys) if key in keys[:index])
                duplicate_key = keys[duplicate_index]
                duplicate_placement = statement_contexts[duplicate_index][1]
                logger.error(
                    "guts_statement_key_invariant_failed validator_rule=%s placement=%s generated_key=%s",
                    "deterministic_statement_key_uniqueness", duplicate_placement, duplicate_key,
                )
                raise GUTSStatementKeyInvariantError(
                    statement_key=duplicate_key, statement_placement=duplicate_placement,
                )
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
        for position, (statement, placement) in enumerate(statement_contexts):
            normalized[statement.statement_key] = self._validate_statement(
                statement, sources=sources, conflict_source_ids=conflict_source_ids,
                manifest=manifest, position=position, placement=placement,
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
            output_schema_version=self.output_schema_version,
            briefing=ValidatedModelBriefing(
                headline=normalized[output.headline.statement_key].model_copy(update={"placement_type": "headline", "position": 0}),
                summary=tuple(normalized[item.statement_key].model_copy(update={"placement_type": "summary", "position": index}) for index, item in enumerate(output.summary_statements)),
                sections=sections,
            ),
            validated_at=datetime.now(timezone.utc),
        )

    def _validate_statement(
        self, statement: ModelOutputStatement, *, sources: dict[str, SourceMetadata],
        conflict_source_ids: set[str], manifest: GUTSManifest, position: int, placement: str,
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
            self._fail(
                "model_citation_invalid", "The response referenced unavailable sources.",
                "Use only source IDs present in the citation contract.",
                invalid_source_ids=tuple(unknown), allowed_source_ids=tuple(sorted(sources)),
            )
        for source_id in statement.source_ids:
            if source_id.casefold() in text.casefold():
                self._fail("model_output_unsafe", "A raw source ID appeared in prose.", "Keep source IDs only in source_ids arrays.")
        for pattern, reason in PROHIBITED_PATTERNS:
            if pattern.search(text):
                if (
                    reason == "recommendation"
                    and statement.confidence == "attributed"
                    and (
                        preserves_attribution(text)
                        or (
                            self.output_schema_version == "guts-output-v2"
                            and statement.attribution is not None
                        )
                    )
                    and not contains_objective_upgrade(text)
                ):
                    continue
                self._fail("model_output_unsafe", f"The response contained prohibited {reason} language.", "Remove recommendations, speculation, markup, and raw citation syntax.")
        cited = [sources[source_id] for source_id in statement.source_ids]
        sentence_split = bool(re.search(r"(?<![A-Z])[.!?]\s+[A-Z]", text))
        if sentence_split or ";" in text:
            self._fail(
                "model_output_unsafe", "A statement contained multiple apparent claims.",
                "If a statement contains more than one complete sentence or more than one "
                "independently supportable idea, split it into multiple statement objects while "
                "preserving each statement's attribution and citations.",
                statement_key=statement.statement_key, statement_placement=placement,
                statement_index=position, statement_confidence=statement.confidence,
                cited_source_ids=tuple(statement.source_ids),
                cited_source_kinds=tuple(sorted(
                    f"{source.source_class}:{source.source_type}" for source in cited
                )),
                validator_rule="single_claim_statement",
                failure_subtype="multiple_sentences" if sentence_split else "semicolon_split",
            )
        classes = {source.source_class for source in cited}
        if statement.confidence == "supported" and not classes.intersection({"current_state", "official_evidence"}):
            self._fail_confidence_source(
                statement, cited=cited, placement=placement,
                message="A supported statement lacked authoritative evidence.",
                feedback="Label organizational claims attributed and cite their organizational sources.",
                required_source_classes=("current_state", "official_evidence"),
            )
        if statement.confidence == "attributed":
            if "organizational_knowledge" not in classes:
                self._fail_confidence_source(
                    statement, cited=cited, placement=placement,
                    message="An attributed statement lacked organizational evidence.",
                    feedback="Attributed statements must cite organizational knowledge.",
                    required_source_classes=("organizational_knowledge",),
                )
            if self.output_schema_version == "guts-output-v2":
                self._validate_v2_attribution(
                    statement, cited=cited, conflict_source_ids=conflict_source_ids,
                    text=text, placement=placement,
                )
            else:
                team_attribution = bool(TEAM_ATTRIBUTION.search(text))
                coordinated_attribution = bool(COORDINATED_ACTOR_ATTRIBUTION.search(text))
                consensus_unsupported = (team_attribution or coordinated_attribution) and (
                    len(statement.source_ids) < 2
                    or bool(set(statement.source_ids).intersection(conflict_source_ids))
                )
                if (
                    contains_objective_upgrade(text)
                    or not preserves_attribution(text)
                    or consensus_unsupported
                ):
                    self._fail_attribution(statement, cited, placement, text)
        elif self.output_schema_version == "guts-output-v2" and statement.attribution is not None:
            self._fail(
                "model_output_unsafe", "A non-attributed statement included attribution.",
                "Use attribution=null for supported and authoritative uncertain statements.",
                statement_key=statement.statement_key, statement_placement=placement,
                statement_confidence=statement.confidence,
                cited_source_ids=tuple(statement.source_ids),
                cited_source_kinds=tuple(sorted(f"{source.source_class}:{source.source_type}" for source in cited)),
                rejected_statement_text=text, validator_rule="attribution_unexpected",
            )
        if statement.confidence == "uncertain":
            if (
                self.output_schema_version == "guts-output-v2"
                and classes == {"organizational_knowledge"}
                and not set(statement.source_ids).intersection(conflict_source_ids)
            ):
                self._fail_confidence_source(
                    statement, cited=cited, placement=placement,
                    message="An organizational uncertainty lacked attribution.",
                    feedback="Represent a person's concern or question as attributed with structured attribution.",
                    required_source_classes=("current_state", "official_evidence", "historical_context"),
                )
            evidence_uncertain = any(
                UNCERTAINTY_CUES.search(source.searchable)
                or bool(source.source and source.source.structured_facts.get("uncertain"))
                for source in cited
            )
            if not evidence_uncertain and not set(statement.source_ids).intersection(conflict_source_ids):
                self._fail_confidence_source(
                    statement, cited=cited, placement=placement,
                    message="An uncertainty lacked supporting evidence.",
                    feedback="Cite evidence that explicitly expresses the unresolved information.",
                )
        if re.search(r"\b(?:because|therefore|as a result)\b", text, re.I) and not any(
            source.source and source.source.structured_facts.get("causal_statement") is True for source in cited
        ):
            self._fail("model_output_unsafe", "Unsupported causal language was used.", "State supported facts separately without inferring causality.")
        self._validate_dates_and_identifiers(
            text, statement.source_ids, sources, manifest,
            statement_key=statement.statement_key, placement=placement,
            confidence=statement.confidence,
        )
        return ModelStatement(
            statement_key=statement.statement_key, placement_type="summary", section_type=None,
            position=position, text=text, importance=statement.importance,
            confidence=statement.confidence, source_ids=tuple(statement.source_ids),
            attribution=statement.attribution,
        )

    def _validate_v2_attribution(
        self, statement: ModelOutputStatement, *, cited: list[SourceMetadata],
        conflict_source_ids: set[str], text: str, placement: str,
    ) -> None:
        attribution = statement.attribution
        if attribution is None:
            self._fail(
                "model_output_unsafe", "An attributed statement lacked structured attribution.",
                "Add structured attribution copied from eligible cited source-author metadata.",
                statement_key=statement.statement_key, statement_placement=placement,
                statement_confidence=statement.confidence,
                cited_source_ids=tuple(statement.source_ids),
                cited_source_kinds=tuple(sorted(f"{source.source_class}:{source.source_type}" for source in cited)),
                rejected_statement_text=text, validator_rule="attribution_missing",
            )
        organizational = [item for item in cited if item.source_class == "organizational_knowledge"]
        if attribution.type == "internal_source":
            if not organizational or any(item.source and item.source.author for item in organizational):
                self._fail(
                    "model_citation_invalid", "Internal-source attribution was unsupported.",
                    "Use internal_source only for eligible cited organizational evidence without a resolvable author.",
                    statement_key=statement.statement_key, statement_placement=placement,
                    statement_confidence=statement.confidence,
                    cited_source_ids=tuple(statement.source_ids),
                    cited_source_kinds=tuple(sorted(f"{source.source_class}:{source.source_type}" for source in cited)),
                    rejected_statement_text=text, validator_rule="unsupported_internal_source",
                )
            if not INTERNAL_ATTRIBUTION.search(text) or contains_objective_upgrade(text):
                self._fail_attribution(statement, cited, placement, text)
            return

        authors = [item.source.author for item in organizational if item.source and item.source.author]
        for actor in attribution.actors:
            matches = [author for author in authors if actor_matches_author(actor, author)]
            if actor.user_id is None and normalize_email(actor.email) is None:
                identities = {author_identity_key(author) for author in matches}
                if len(identities) > 1:
                    self._fail(
                        "model_citation_invalid", "An actor identity was ambiguous.",
                        "Use exact source-author metadata and do not guess an ambiguous actor.",
                        statement_key=statement.statement_key, statement_placement=placement,
                        statement_confidence=statement.confidence,
                        cited_source_ids=tuple(statement.source_ids),
                        cited_source_kinds=tuple(sorted(f"{source.source_class}:{source.source_type}" for source in cited)),
                        rejected_statement_text=text, validator_rule="ambiguous_actor_identity",
                    )
            if not matches:
                self._fail(
                    "model_citation_invalid", "An actor did not match cited evidence.",
                    "Copy each actor exactly from eligible cited source-author metadata.",
                    statement_key=statement.statement_key, statement_placement=placement,
                    statement_confidence=statement.confidence,
                    cited_source_ids=tuple(statement.source_ids),
                    cited_source_kinds=tuple(sorted(f"{source.source_class}:{source.source_type}" for source in cited)),
                    rejected_statement_text=text, validator_rule="actor_source_mismatch",
                )
            name_key = normalized_name_key(actor.display_name)
            if name_key and name_key not in text.casefold():
                self._fail_attribution(statement, cited, placement, text)

        consensus_wording = bool(TEAM_ATTRIBUTION.search(text) or COORDINATED_ACTOR_ATTRIBUTION.search(text))
        if consensus_wording and (
            len(attribution.actors) < 2
            or len(organizational) < 2
            or bool(set(statement.source_ids).intersection(conflict_source_ids))
        ):
            self._fail(
                "model_output_unsafe", "The statement implied unsupported consensus.",
                "Use team wording only for multiple consistent cited actors.",
                statement_key=statement.statement_key, statement_placement=placement,
                statement_confidence=statement.confidence,
                cited_source_ids=tuple(statement.source_ids),
                cited_source_kinds=tuple(sorted(f"{source.source_class}:{source.source_type}" for source in cited)),
                rejected_statement_text=text, validator_rule="consensus_scope_invalid",
            )
        if contains_objective_upgrade(text):
            self._fail_attribution(statement, cited, placement, text)

    def _fail_attribution(
        self, statement: ModelOutputStatement, cited: list[SourceMetadata],
        placement: str, text: str,
    ) -> None:
        self._fail(
            "model_output_unsafe", "An attributed claim did not preserve attribution.",
            "Name the person who made the recommendation, plan, concern, or observation; do not generalize one person's statement into organizational consensus; use concise actor-attributed wording.",
            statement_key=statement.statement_key, statement_placement=placement,
            statement_confidence=statement.confidence,
            cited_source_ids=tuple(statement.source_ids),
            cited_source_kinds=tuple(sorted(f"{source.source_class}:{source.source_type}" for source in cited)),
            rejected_statement_text=text, validator_rule="attribution_preservation",
            multiple_cited_actors=_multiple_communication_actors(cited),
        )

    def _fail_confidence_source(
        self, statement: ModelOutputStatement, *, cited: list[SourceMetadata],
        placement: str, message: str, feedback: str,
        required_source_classes: tuple[str, ...] = (),
    ) -> None:
        self._fail(
            "model_citation_invalid", message, feedback,
            statement_key=statement.statement_key, statement_placement=placement,
            statement_confidence=statement.confidence,
            cited_source_ids=tuple(statement.source_ids),
            cited_source_kinds=tuple(sorted(
                f"{source.source_class}:{source.source_type}" for source in cited
            )),
            rejected_statement_text=_normalize_text(statement.text),
            validator_rule="confidence_source_compatibility",
            required_source_classes=required_source_classes,
        )

    def _validate_section(self, section_type: str, statements: Iterable[ModelOutputStatement], sources: dict[str, SourceMetadata]) -> None:
        for statement in statements:
            cited = [sources[source_id] for source_id in statement.source_ids if source_id in sources]
            classes = {source.source_class for source in cited}
            valid = {
                "current_state": bool(classes.intersection({"current_state", "official_evidence"})),
                "official_updates": bool(classes.intersection({"official_evidence", "historical_context"})),
                "organizational_knowledge": "organizational_knowledge" in classes,
                "important_history": "historical_context" in classes,
                "uncertainties": statement.confidence == "uncertain",
            }[section_type]
            if not valid:
                self._fail(
                    "model_citation_invalid", "A section did not match its cited evidence.",
                    "Place statements only in sections compatible with their source classes.",
                    statement_key=statement.statement_key,
                    statement_placement=f"section:{section_type}",
                    statement_confidence=statement.confidence,
                    cited_source_ids=tuple(statement.source_ids),
                    cited_source_kinds=tuple(sorted(
                        f"{source.source_class}:{source.source_type}" for source in cited
                    )),
                    rejected_statement_text=_normalize_text(statement.text),
                    validator_rule="section_source_compatibility",
                    allowed_source_classes=SECTION_ALLOWED_SOURCE_CLASSES[section_type],
                )
            if section_type == "uncertainties" and statement.confidence != "uncertain":
                self._fail("model_citation_invalid", "An uncertainty section used the wrong confidence.", "Use uncertain confidence for every uncertainties statement.")

    def _validate_dates_and_identifiers(
        self, text: str, source_ids: tuple[str, ...], sources: dict[str, SourceMetadata],
        manifest: GUTSManifest, *, statement_key: str, placement: str, confidence: str,
    ) -> None:
        grounded_fields: list[tuple[str, str]] = []
        deadline = manifest.current_state.response_deadline.value
        if deadline:
            deadline_variants = _date_variants(str(deadline))
            if any(variant in text.casefold() for variant in deadline_variants):
                required = manifest.current_state.response_deadline.source_id
                if required not in source_ids:
                    self._fail_grounded_field(
                        "The current deadline used the wrong citation.", statement_key=statement_key,
                        placement=placement, field_name="response_deadline", required_source_id=required,
                        cited_source_ids=source_ids, text=text, confidence=confidence, sources=sources,
                    )
                grounded_fields.append(("response_deadline", required))
        solicitation = manifest.current_state.solicitation_number.value
        if solicitation and str(solicitation).casefold() in text.casefold():
            required = manifest.current_state.solicitation_number.source_id
            if required not in source_ids:
                self._fail_grounded_field(
                    "The solicitation number used the wrong citation.", statement_key=statement_key,
                    placement=placement, field_name="solicitation_number", required_source_id=required,
                    cited_source_ids=source_ids, text=text, confidence=confidence, sources=sources,
                )
            grounded_fields.append(("solicitation_number", required))
        stage = manifest.current_state.source_stage.value
        if stage and re.search(rf"(?<!\w){re.escape(str(stage))}(?!\w)", text, re.I):
            required = manifest.current_state.source_stage.source_id
            if required not in source_ids:
                self._fail_grounded_field(
                    "The current source stage used the wrong citation.", statement_key=statement_key,
                    placement=placement, field_name="source_stage", required_source_id=required,
                    cited_source_ids=source_ids, text=text, confidence=confidence, sources=sources,
                )
            grounded_fields.append(("source_stage", required))
        if grounded_fields and any(sources[source_id].source_class == "organizational_knowledge" for source_id in source_ids):
            field_name, required = grounded_fields[0]
            self._fail(
                "model_output_unsafe", "A current-state field was combined with organizational knowledge.",
                "Split current-state fields and organizational claims into separate atomic statements.",
                statement_key=statement_key, statement_placement=placement,
                grounded_field=field_name, required_source_id=required, cited_source_ids=source_ids,
            )
        cited_searchable = " ".join(sources[source_id].searchable for source_id in source_ids).casefold()
        for match in DATE_PATTERN.findall(text):
            if not any(variant in cited_searchable for variant in _date_variants(match)):
                self._fail(
                    "model_citation_invalid", "A date was not grounded by its citations.",
                    "Cite the source containing each exact date.",
                    statement_key=statement_key, statement_placement=placement,
                    grounded_field="exact_date", cited_source_ids=source_ids,
                    statement_confidence=confidence,
                    cited_source_kinds=tuple(sorted(
                        f"{sources[source_id].source_class}:{sources[source_id].source_type}"
                        for source_id in source_ids
                    )),
                    rejected_statement_text=text, validator_rule="exact_date_grounding",
                )

    def _fail_grounded_field(
        self, message: str, *, statement_key: str, placement: str, field_name: str,
        required_source_id: str, cited_source_ids: tuple[str, ...], text: str,
        confidence: str, sources: dict[str, SourceMetadata],
    ) -> None:
        self._fail(
            "model_citation_invalid", message,
            "Use the exact required current-state field citation and split unrelated claims.",
            statement_key=statement_key, statement_placement=placement,
            grounded_field=field_name, required_source_id=required_source_id,
            required_source_ids=(required_source_id,), cited_source_ids=cited_source_ids,
            statement_confidence=confidence,
            cited_source_kinds=tuple(sorted(
                f"{sources[source_id].source_class}:{sources[source_id].source_type}"
                for source_id in cited_source_ids
            )),
            rejected_statement_text=text, validator_rule="current_state_field_grounding",
        )

    @staticmethod
    def _fail(
        category: str, message: str, feedback: str, *,
        invalid_source_ids: tuple[str, ...] = (), allowed_source_ids: tuple[str, ...] = (),
        statement_key: str | None = None, statement_placement: str | None = None,
        grounded_field: str | None = None, required_source_id: str | None = None,
        required_source_ids: tuple[str, ...] = (),
        cited_source_ids: tuple[str, ...] = (),
        statement_confidence: str | None = None, cited_source_kinds: tuple[str, ...] = (),
        rejected_statement_text: str | None = None, validator_rule: str | None = None,
        allowed_source_classes: tuple[str, ...] = (),
        required_source_classes: tuple[str, ...] = (),
        multiple_cited_actors: bool = False,
        statement_index: int | None = None, failure_subtype: str | None = None,
    ):
        raise GUTSValidationError(
            category, message, feedback, invalid_source_ids=invalid_source_ids,
            allowed_source_ids=allowed_source_ids, statement_key=statement_key,
            statement_placement=statement_placement, grounded_field=grounded_field,
            required_source_id=required_source_id, required_source_ids=required_source_ids,
            cited_source_ids=cited_source_ids,
            statement_confidence=statement_confidence, cited_source_kinds=cited_source_kinds,
            rejected_statement_text=rejected_statement_text, validator_rule=validator_rule,
            allowed_source_classes=allowed_source_classes,
            required_source_classes=required_source_classes,
            multiple_cited_actors=multiple_cited_actors,
            statement_index=statement_index, failure_subtype=failure_subtype,
        )
