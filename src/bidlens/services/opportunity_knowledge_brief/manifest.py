"""Validated GUTS manifest construction, canonicalization, and hashing."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import unicodedata
from typing import Any

from .contracts import (
    CurrentOpportunityState, FinalEvidenceSelection, GUTSManifest, GenerationConstraints,
    current_state_source_id,
)


class ManifestValidationError(ValueError):
    pass


class ManifestBuilder:
    def build(
        self, *, manifest_version: str, current_state: CurrentOpportunityState,
        selection: FinalEvidenceSelection, constraints: GenerationConstraints,
        snapshot_started_at: datetime, snapshot_completed_at: datetime,
    ) -> GUTSManifest:
        sources = selection.selection.sources
        source_ids = [source.source_id for source in sources]
        if len(source_ids) != len(set(source_ids)):
            raise ManifestValidationError("Evidence source IDs must be unique.")
        if any(not source.text.strip() for source in sources):
            raise ManifestValidationError("Evidence text cannot be empty.")
        for field_name in (
            "title", "client", "description", "response_deadline", "posted_date",
            "solicitation_number", "opportunity_type", "source_stage", "source",
            "source_record_id", "source_url", "sam_url", "bidlens_id", "sam_notice_id",
            "naics", "naics_title", "set_aside",
        ):
            if getattr(current_state, field_name).source_id != current_state_source_id(
                current_state.opportunity_id, field_name
            ):
                raise ManifestValidationError("Current-state source IDs are not controlled.")
        for source in sources:
            scope = source.provenance
            if scope.get("organization_id", current_state.organization_id) != current_state.organization_id:
                raise ManifestValidationError("Evidence organization scope is inconsistent.")
            if scope.get("workspace_id", current_state.workspace_id) != current_state.workspace_id:
                raise ManifestValidationError("Evidence workspace scope is inconsistent.")
            if scope.get("opportunity_id", current_state.opportunity_id) != current_state.opportunity_id:
                raise ManifestValidationError("Evidence opportunity scope is inconsistent.")
        for unavailable in selection.selection.unavailable_sources:
            scope = unavailable.provenance
            if scope.get("organization_id", current_state.organization_id) != current_state.organization_id:
                raise ManifestValidationError("Unavailable-source organization scope is inconsistent.")
        total = len(current_state.canonical_json()) + selection.selection.statistics.selected_character_count
        if total > constraints.max_total_input_characters:
            raise ManifestValidationError("Selected manifest content exceeds the total character budget.")
        return GUTSManifest(
            manifest_version=manifest_version, opportunity_id=current_state.opportunity_id,
            organization_id=current_state.organization_id, workspace_id=current_state.workspace_id,
            snapshot_started_at=snapshot_started_at, snapshot_completed_at=snapshot_completed_at,
            current_state=current_state, evidence=selection.selection, constraints=constraints,
            reproducibility_status=selection.reproducibility_status,
        )


def _normalize(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFKC", value)
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items() if key not in {
            "snapshot_started_at", "snapshot_completed_at", "retrieved_at", "retrieval_duration_ms",
        }}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


class ManifestCanonicalizer:
    """Canonical fingerprint excludes volatile snapshot/retrieval timing metadata."""

    def canonical_bytes(self, manifest: GUTSManifest) -> bytes:
        normalized = _normalize(manifest.serializable_dict())
        return json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class ManifestHasher:
    def __init__(self, canonicalizer: ManifestCanonicalizer | None = None):
        self.canonicalizer = canonicalizer or ManifestCanonicalizer()

    def hash(self, manifest: GUTSManifest) -> str:
        return hashlib.sha256(self.canonicalizer.canonical_bytes(manifest)).hexdigest()
