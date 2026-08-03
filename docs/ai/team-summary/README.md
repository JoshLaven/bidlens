# Team Summary

## Product boundary

Team Summary is the planned human-readable account of what an opportunity team has communicated or recorded. It evolves the existing Communication Summary pathway rather than creating a third summarization system.

Eligible evidence is limited to:

- stored opportunity communications; and
- current opportunity notes.

Team Summary explicitly excludes current-state fields, opportunity metadata, solicitation descriptions, official documents, amendments, deadline changes, funding and eligibility facts, solicitation history, official update events, and prior AI output. Those sources remain the responsibility of the Opportunity Folder and the separate Recent Developments pathway.

## Canonical narrative lifecycle

Team Summary is now the sole human-readable organizational narrative. The former Communication Summary service, route, and unique mutable persistence row have evolved additively into Team Summary so existing links and rows remain compatible. No parallel Overview summary or append-only Team Summary generation was introduced.

One selected bundle of stored communications and notes produces one integrated narrative. The same persisted record is rendered on Overview and Communication; Communication continues to place the unchanged email Timeline directly beneath it. Overview includes a non-functional placeholder for a separately scoped future Official Updates capability.

The model receives no opportunity metadata, solicitation description, documents, official history, previous AI summaries, or GUTS output. When a person mentions an apparent official fact, the prompt requires attributed phrasing rather than presenting it as objective truth.

## Relationship to GUTS

GUTS remains the independent, append-only structured-memory and validation engine. It continues to collect current state, official evidence, organizational knowledge, and history; generate atomic attributed statements; validate citations and attribution; and persist sources, statements, and provenance.

Team Summary does not call the GUTS compiler. GUTS does not call the Communication Summary service. They share only low-level, deterministic preparation such as message cleanup and identity normalization. GUTS authorization, generation, validation, and persistence behavior remain unchanged.

## Shared organizational-evidence contracts

The Phase 1 contracts are database-independent:

- `OrganizationalEvidenceAuthor` records an internal user, external person, or authorless source.
- `OrganizationalEvidenceItem` represents one cleaned communication or note with stable source identity, timestamps, author snapshot, content hash, and source-specific metadata.
- `OrganizationalEvidenceSelectionPolicy` makes caller-specific count and character limits explicit.
- `OrganizationalEvidenceCollection` reports selected items, availability, omissions, character counts, and truncation.
- `TeamSummaryEvidenceBundle` combines selected communications and notes with scope, counts, policies, omissions, and a deterministic evidence fingerprint.

The contracts do not depend on GUTS provider output, the GUTS compiler, or GUTS persistence models.

## Cleanup and filtering

Shared communication cleanup performs only proven deterministic transformations: HTML-to-text conversion, quoted-reply removal, signature cutoff, mobile-client footer removal, and whitespace normalization. Classification of acknowledgements, delivery notifications, automated messages, signature-only content, and empty content is separate so callers can retain their existing policies.

No semantic cleaning, fuzzy matching, model ranking, or source interpretation occurs in this layer.

## Ordering, selection, and deduplication

Collectors enforce organization, workspace, and opportunity scope and read stored BidLens rows only. They never retrieve messages from Microsoft Graph.

Within a source type:

1. Communication provider/message identities are deduplicated first.
2. Exact normalized content is deduplicated according to the caller policy.
3. Sources are ordered chronologically with stable source IDs as tie-breakers.
4. Over-budget selection preserves earliest context, recent sources, and deterministic interior coverage.
5. Caller-provided policies retain the different Communication Summary and GUTS limits.

When selected note and communication evidence is combined for a future Team Summary, an exact same-actor duplicate prefers the note. Identical content from different actors is preserved. Similar but non-identical content is preserved. Application code performs no fuzzy semantic deduplication.

GUTS retains its existing source-specific exact-content behavior during Phase 1 for backward compatibility.

## Evidence fingerprint

The Team Summary evidence fingerprint is SHA-256 over canonical JSON containing only:

- input-contract version;
- organization, workspace, and opportunity IDs;
- ordered selected source IDs and source types;
- content hashes;
- normalized source and update timestamps;
- author identity fingerprints;
- a hash of bounded stable identity metadata;
- selection-policy versions and limits;
- available and selected counts;
- omission and truncation metadata; and
- selected and original character counts.

The fingerprint payload excludes source text, note bodies, message bodies, subjects, recipient addresses, and actor email addresses. Repeated collection of unchanged evidence produces the same fingerprint. Relevant edits, additions, deletions, author changes, source-order changes, and policy changes produce a different fingerprint.

The live Team Summary persists this fingerprint with the input-contract version and Team Summary prompt version. A row is stale when any of those values changes. A provider model-name change alone does not invalidate a summary. Legacy rows have no fingerprint/version metadata and therefore become naturally stale without a destructive migration.

## Product boundaries

The Timeline remains the source-evidence reading surface. Official Updates will be implemented separately in a later phase. GUTS remains the independent structured-memory engine and is neither called by nor required for Team Summary generation.
