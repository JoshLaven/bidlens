"""Stable string values used by Get Up to Speed persistence contracts."""


class GenerationStatus:
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


GENERATION_STATUSES = frozenset({
    GenerationStatus.PENDING,
    GenerationStatus.RUNNING,
    GenerationStatus.SUCCEEDED,
    GenerationStatus.FAILED,
})


class ExtractionStatus:
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


EXTRACTION_STATUSES = frozenset({
    ExtractionStatus.PENDING,
    ExtractionStatus.SUCCEEDED,
    ExtractionStatus.FAILED,
})


class ReproducibilityStatus:
    FULLY_REPRODUCIBLE = "fully_reproducible"
    PARTIALLY_REPRODUCIBLE = "partially_reproducible"
    NOT_REPRODUCIBLE = "not_reproducible"


REPRODUCIBILITY_STATUSES = frozenset({
    ReproducibilityStatus.FULLY_REPRODUCIBLE,
    ReproducibilityStatus.PARTIALLY_REPRODUCIBLE,
    ReproducibilityStatus.NOT_REPRODUCIBLE,
})


class SourceClass:
    CURRENT_STATE = "current_state"
    OFFICIAL_EVIDENCE = "official_evidence"
    ORGANIZATIONAL_KNOWLEDGE = "organizational_knowledge"
    HISTORICAL_CONTEXT = "historical_context"


SOURCE_CLASSES = frozenset({
    SourceClass.CURRENT_STATE,
    SourceClass.OFFICIAL_EVIDENCE,
    SourceClass.ORGANIZATIONAL_KNOWLEDGE,
    SourceClass.HISTORICAL_CONTEXT,
})


class Authority:
    AUTHORITATIVE_CURRENT = "authoritative_current"
    OFFICIAL_SOURCE = "official_source"
    ATTRIBUTED_CLAIM = "attributed_claim"
    HISTORICAL_RECORD = "historical_record"


AUTHORITIES = frozenset({
    Authority.AUTHORITATIVE_CURRENT,
    Authority.OFFICIAL_SOURCE,
    Authority.ATTRIBUTED_CLAIM,
    Authority.HISTORICAL_RECORD,
})


class PlacementType:
    HEADLINE = "headline"
    SUMMARY = "summary"
    SECTION = "section"


PLACEMENT_TYPES = frozenset({PlacementType.HEADLINE, PlacementType.SUMMARY, PlacementType.SECTION})


class SectionType:
    CURRENT_STATE = "current_state"
    OFFICIAL_UPDATES = "official_updates"
    ORGANIZATIONAL_KNOWLEDGE = "organizational_knowledge"
    IMPORTANT_HISTORY = "important_history"
    UNCERTAINTIES = "uncertainties"


SECTION_TYPES = frozenset({
    SectionType.CURRENT_STATE,
    SectionType.OFFICIAL_UPDATES,
    SectionType.ORGANIZATIONAL_KNOWLEDGE,
    SectionType.IMPORTANT_HISTORY,
    SectionType.UNCERTAINTIES,
})


class Importance:
    HIGH = "high"
    NORMAL = "normal"


IMPORTANCE_VALUES = frozenset({Importance.HIGH, Importance.NORMAL})


class Confidence:
    SUPPORTED = "supported"
    ATTRIBUTED = "attributed"
    UNCERTAIN = "uncertain"


CONFIDENCE_VALUES = frozenset({Confidence.SUPPORTED, Confidence.ATTRIBUTED, Confidence.UNCERTAIN})


class WarningType:
    MISSING_SOURCE = "missing_source"
    PARTIAL_GENERATION = "partial_generation"
    CONFLICTING_SOURCES = "conflicting_sources"
    TRUNCATED_INPUT = "truncated_input"
    NOT_FULLY_REPRODUCIBLE = "not_fully_reproducible"


WARNING_TYPES = frozenset({
    WarningType.MISSING_SOURCE,
    WarningType.PARTIAL_GENERATION,
    WarningType.CONFLICTING_SOURCES,
    WarningType.TRUNCATED_INPUT,
    WarningType.NOT_FULLY_REPRODUCIBLE,
})


class FailureCategory:
    ACCESS_DENIED = "access_denied"
    SHORTLIST_REQUIRED = "shortlist_required"
    OPPORTUNITY_NOT_FOUND = "opportunity_not_found"
    SOURCE_COLLECTION_FAILED = "source_collection_failed"
    SOURCE_RETRIEVAL_FAILED = "source_retrieval_failed"
    SOURCE_PARSE_FAILED = "source_parse_failed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    MANIFEST_BUILD_FAILED = "manifest_build_failed"
    MANIFEST_VALIDATION_FAILED = "manifest_validation_failed"
    MODEL_CONFIGURATION_MISSING = "model_configuration_missing"
    MODEL_TIMEOUT = "model_timeout"
    MODEL_PROVIDER_ERROR = "model_provider_error"
    MODEL_SCHEMA_INVALID = "model_schema_invalid"
    MODEL_CITATION_INVALID = "model_citation_invalid"
    MODEL_OUTPUT_UNSAFE = "model_output_unsafe"
    PERSISTENCE_FAILED = "persistence_failed"
    GENERATION_ALREADY_IN_PROGRESS = "generation_already_in_progress"
    STALE_ATTEMPT = "stale_attempt"
    UNEXPECTED_ERROR = "unexpected_error"


FAILURE_CATEGORIES = frozenset({
    value for name, value in vars(FailureCategory).items() if name.isupper()
})
