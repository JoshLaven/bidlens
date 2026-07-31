# Input Manifest

`ManifestBuilder` now builds the versioned, deterministic manifest that a future compiler will use before any model call.

## Manifest header

- Manifest version and opportunity identity
- Organization/workspace scope
- Snapshot start/completion timestamps
- Current-state snapshot
- Source counts, truncation warnings, and reproducibility status
- Stable hash of the normalized manifest
- Fixed briefing goal and generation constraints

## Source entry

Each entry must have a machine-readable `source_id`, `source_class`, `source_type`, `authority`, citation label, provenance, relevant timestamps, retention status, character counts, truncation state, and content hash where available.

Source text is bounded compiler input. It is not stored on generation/source persistence rows. Raw prompts, raw provider responses, and combined manifest text are never persisted.

## Determinism

Normalize timestamps, ordering, whitespace, JSON keys, and source identifiers before hashing. The same evidence snapshot and compiler version should produce the same manifest hash even if the eventual model output differs.

`ManifestCanonicalizer` emits compact, sorted-key UTF-8 JSON with NFKC-normalized strings and explicit UTC datetime serialization inherited from the contracts. Evidence ordering is fixed by class precedence, occurrence time, and source ID. Snapshot start/completion, external retrieval timestamps, and retrieval-duration values are excluded from the fingerprint as volatile metadata. The fingerprint includes the manifest version, exact selected text, source IDs/hashes, current state, conflicts, unavailable sources, and generation constraints. Prompt and output-schema versions are not yet manifest fields and therefore are not included; generation persistence records them separately. `ManifestHasher` computes SHA-256 and never logs canonical bytes.

Validation requires unique source IDs, nonempty evidence, matching organization/workspace/opportunity provenance, consistent character totals, controlled current-state source IDs through the Session 2 contract, and compliance with the final total-character budget.
