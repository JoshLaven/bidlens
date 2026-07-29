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
    SourceMaterialValidationError,
    create_draft,
    expire_abandoned_drafts,
    get_draft,
    preserve_materials_for_opportunity,
    store_source_material,
    update_draft,
)
from .storage import (
    LocalSourceMaterialStorage,
    SourceMaterialStorage,
    SourceMaterialStorageError,
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

__all__ = [
    "DEFAULT_ADD_TO_SHORTLIST",
    "DraftAccessError",
    "DraftCleanupResult",
    "DuplicateCheckResult",
    "DuplicateMatch",
    "INTAKE_DECISION_STATE",
    "INTAKE_QUALIFICATION_STATUS",
    "INTAKE_SOURCE",
    "IntakeCandidate",
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
    "LocalSourceMaterialStorage",
    "SourceMaterialStorage",
    "SourceMaterialStorageError",
    "SourceMaterialValidationError",
    "ValidationError",
    "format_internal_reference",
    "configured_source_material_storage",
    "create_draft",
    "expire_abandoned_drafts",
    "find_publication_duplicates",
    "generate_storage_key",
    "get_draft",
    "intake_csrf_token",
    "normalize_candidate",
    "normalize_duplicate_key",
    "opportunity_creation_defaults",
    "opportunity_field_values",
    "preserve_materials_for_opportunity",
    "sanitize_original_filename",
    "store_source_material",
    "update_draft",
    "validate_candidate",
    "validate_intake_csrf_token",
]
