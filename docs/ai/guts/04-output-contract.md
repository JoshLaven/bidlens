# Output Contract

Canonical output is validated structured JSON. Rendering is application-controlled. The configured `GUTS_PROMPT_VERSION` defaults to `guts-v2`; this version supplies a dedicated `allowed_source_ids` inventory for exact citation selection. Output continues to use `GUTS_OUTPUT_SCHEMA_VERSION`.

## Provider output

The strict provider object contains only `headline`, non-empty `summary_statements`, and `sections`. Statements contain `statement_key`, plain `text`, controlled `importance` and `confidence`, and a non-empty `source_ids` array. Sections contain only a controlled `section_type` and non-empty statements. The provider cannot return titles, placement, position, warnings, statistics, HTML, Markdown, or metadata. BidLens assigns placement, section display title, and position after validation.

## Statements

Every visible claim is an atomic statement with:

- Stable `statement_key`
- Placement: `headline`, `summary`, or `section`
- Optional section type/title
- Position
- Plain text
- Importance: `high` or `normal`
- Confidence: `supported`, `attributed`, or `uncertain`
- One or more machine-readable source IDs

Sections are limited to current state, official updates, organizational knowledge, important history, and uncertainties.

## Validation invariants

- Source IDs and statement keys are unique within a generation.
- Every statement has at least one citation.
- Every citation resolves to an included source.
- Output contains no uncited prose.
- Internal claims remain attributed.
- Unsupported or unsafe output fails validation and is not stored as canonical output.

Warnings may report missing sources, partial generation, conflicts, truncation, or incomplete reproducibility.

## Validation

The validator enforces one `headline` key with high importance, at least one summary statement, unique statement keys and sections, configured statement/section/count/character limits, a 500-word hard maximum, plain atomic prose, and deterministic section ordering.

Citation compatibility:

- `supported` requires current-state or official evidence.
- `attributed` requires organizational evidence and attribution-preserving language.
- `uncertain` requires an involved known-conflict source or evidence explicitly expressing uncertainty.
- Current State uses current/official citations.
- Official Updates uses official or material historical citations.
- Organizational Knowledge requires organizational citations.
- Important History requires historical citations.
- Uncertainties requires uncertain confidence and supporting evidence.

The validator rejects unknown/duplicate/missing citations, section/source mismatches, ungrounded dates and identifiers where deterministic checks are reliable, wrong operative-deadline citations, obvious multi-sentence or semicolon-combined claims, recommendations, bid advice, speculation, unsupported causality, AI self-reference, Markdown/HTML, citation markup, and raw source IDs in prose. It normalizes harmless whitespace but never rewrites claims, changes confidence, invents citations, drops invalid statements, or merges statements.

## Corrective retry

Exactly one corrective retry is allowed for schema, citation, atomicity, length, section, or safety validation failures. It receives the unchanged manifest and outer prompt plus one short allowlisted validator instruction. Raw output, exceptions, source text, prompts, and stack traces are never included in feedback. A second invalid result fails safely. There is no deterministic prose fallback.
