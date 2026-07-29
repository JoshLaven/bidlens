"""Shared domain primitives for user-created opportunity intake.

Phase 1 is intentionally pure: it does not access the database, files, routes,
or external providers. Later intake adapters should converge on these contracts.
"""

from .contracts import (
    DEFAULT_ADD_TO_SHORTLIST,
    INTAKE_DECISION_STATE,
    INTAKE_QUALIFICATION_STATUS,
    INTAKE_SOURCE,
    IntakeCandidate,
    IntakeMethod,
    OpportunityPublishCommand,
    OpportunityPublishResult,
)
from .normalization import normalize_candidate, opportunity_creation_defaults, opportunity_field_values
from .references import format_internal_reference
from .validation import IntakeValidationResult, ValidationError, validate_candidate
from .drafts import (
    DraftAccessError,
    DraftCleanupResult,
    SourceMaterialReconciliationReport,
    SourceMaterialValidationError,
    create_draft,
    expire_abandoned_drafts,
    get_draft,
    preserve_materials_for_opportunity,
    reconcile_source_materials,
    store_source_material,
    update_draft,
)
from .storage import (
    LocalSourceMaterialStorage,
    S3SourceMaterialStorage,
    SourceMaterialStorage,
    SourceMaterialStorageError,
    StorageObjectMetadata,
    cleanup_uploaded_objects,
    configured_source_material_storage,
    generate_storage_key,
    sanitize_original_filename,
)
from .duplicates import (
    DuplicateCheckResult,
    DuplicateMatch,
    find_publication_duplicates,
    normalize_duplicate_key,
)
from .publisher import (
    OpportunityDuplicateError,
    OpportunityPublicationAccessError,
    OpportunityPublicationConflict,
    OpportunityPublicationError,
    OpportunityPublicationValidationError,
    OpportunityPublisher,
)
from .csrf import intake_csrf_token, validate_intake_csrf_token
from .document_parsing import (
    IntakeDocumentError,
    ParsedIntakeDocument,
    parse_intake_document,
    validate_intake_document,
)
from .document_extraction import (
    IntakeDocumentExtractor,
    IntakeExtractionError,
    IntakeExtractionResult,
    OpenAIIntakeDocumentExtractor,
    extraction_schema,
    parse_extraction_payload,
)
from .document_upload import DocumentUploadResult, process_rfp_document
from .email_parsing import (
    IntakeEmailError,
    ParsedEmailAttachment,
    ParsedIntakeEmail,
    parse_intake_email,
    validate_intake_email,
)
from .email_extraction import (
    IntakeEmailExtractionResult,
    IntakeEmailExtractor,
    OpenAIIntakeEmailExtractor,
    email_extraction_schema,
    parse_email_extraction_payload,
)
from .email_upload import EmailUploadResult, assemble_email_extraction_input, process_email_file

__all__ = [
    "DEFAULT_ADD_TO_SHORTLIST",
    "DraftAccessError",
    "DraftCleanupResult",
    "DuplicateCheckResult",
    "DuplicateMatch",
    "DocumentUploadResult",
    "EmailUploadResult",
    "INTAKE_DECISION_STATE",
    "INTAKE_QUALIFICATION_STATUS",
    "INTAKE_SOURCE",
    "IntakeCandidate",
    "IntakeDocumentError",
    "IntakeDocumentExtractor",
    "IntakeEmailError",
    "IntakeEmailExtractionResult",
    "IntakeEmailExtractor",
    "IntakeExtractionError",
    "IntakeExtractionResult",
    "IntakeMethod",
    "IntakeValidationResult",
    "OpportunityPublishCommand",
    "OpportunityPublishResult",
    "OpportunityDuplicateError",
    "OpportunityPublicationAccessError",
    "OpportunityPublicationConflict",
    "OpportunityPublicationError",
    "OpportunityPublicationValidationError",
    "OpportunityPublisher",
    "OpenAIIntakeDocumentExtractor",
    "OpenAIIntakeEmailExtractor",
    "ParsedEmailAttachment",
    "ParsedIntakeEmail",
    "ParsedIntakeDocument",
    "LocalSourceMaterialStorage",
    "S3SourceMaterialStorage",
    "SourceMaterialStorage",
    "SourceMaterialStorageError",
    "SourceMaterialReconciliationReport",
    "SourceMaterialValidationError",
    "StorageObjectMetadata",
    "ValidationError",
    "format_internal_reference",
    "configured_source_material_storage",
    "cleanup_uploaded_objects",
    "create_draft",
    "email_extraction_schema",
    "expire_abandoned_drafts",
    "extraction_schema",
    "find_publication_duplicates",
    "generate_storage_key",
    "get_draft",
    "intake_csrf_token",
    "normalize_candidate",
    "normalize_duplicate_key",
    "opportunity_creation_defaults",
    "opportunity_field_values",
    "parse_extraction_payload",
    "parse_email_extraction_payload",
    "parse_intake_email",
    "parse_intake_document",
    "preserve_materials_for_opportunity",
    "reconcile_source_materials",
    "process_rfp_document",
    "process_email_file",
    "sanitize_original_filename",
    "store_source_material",
    "update_draft",
    "validate_candidate",
    "validate_intake_document",
    "validate_intake_email",
    "validate_intake_csrf_token",
    "assemble_email_extraction_input",
]
