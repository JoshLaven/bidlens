# Output Contract

Canonical output is validated structured JSON. Rendering is application-controlled. The configured `GUTS_PROMPT_VERSION` defaults to `guts-v9`; this version retains the structured citation contract and preserves distinct actor provenance when multiple communications contribute different organizational ideas. Output continues to use `GUTS_OUTPUT_SCHEMA_VERSION`.

## Provider output

The strict provider object contains only `headline`, non-empty `summary_statements`, and `sections`. In V2, provider statements contain plain `text`, controlled `importance` and `confidence`, a non-empty `source_ids` array, and required nullable attribution; they neither accept nor generate `statement_key`. Sections contain only a controlled `section_type` and non-empty statements. Immediately after provider parsing, BidLens assigns application-owned deterministic keys from placement and one-based position (`headline`, `summary_1`, and `<section_type>_1`) before semantic validation. The provider cannot return titles, placement, position, warnings, statistics, HTML, Markdown, or metadata. Historical V1 provider and persisted keys remain unchanged.

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

- Source IDs and canonical application-owned statement keys are unique within a generation.
- Every statement has at least one citation.
- Every citation resolves to an included source.
- Output contains no uncited prose.
- Internal claims remain attributed.
- Organizational claims preserve provenance, actor or explicit source framing, material meaning, and epistemic status. BidLens validates the faithful attributed representation, not whether the speaker's opinion or assessment is objectively correct.
- Unsupported or unsafe output fails validation and is not stored as canonical output.

Warnings may report missing sources, partial generation, conflicts, truncation, or incomplete reproducibility.

## Validation

The validator enforces one `headline` key with high importance, at least one summary statement, unique statement keys and sections, configured statement/section/count/character limits, a 500-word hard maximum, plain atomic prose, and deterministic section ordering.

Citation compatibility:

- `supported` requires current-state or official evidence.
- `attributed` requires organizational evidence and attribution-preserving language.
- The V1 runtime currently recognizes attribution through deterministic prose patterns. The V2 organizational-knowledge direction is structured attribution verified against cited source-author metadata; see [GUTS Organizational Knowledge Contract V2](10-organizational-knowledge-contract.md).
- The locked V2 shape, validation matrix, persistence compatibility, and versioning plan are defined in [Structured Attribution V2 Design](11-structured-attribution-design.md). Phase B will require structured attribution on new attributed output while preserving historical V1 prose-only generations.
- `uncertain` requires an involved known-conflict source or evidence explicitly expressing uncertainty.
- Current State uses current/official citations.
- Official Updates uses official or material historical citations.
- Organizational Knowledge requires organizational citations.
- Important History requires historical citations.
- Uncertainties requires uncertain confidence and supporting evidence.

The validator rejects unknown/duplicate/missing citations, section/source mismatches, ungrounded dates and identifiers where deterministic checks are reliable, wrong operative-deadline citations, obvious multi-sentence or semicolon-combined claims, recommendations, bid advice, speculation, unsupported causality, AI self-reference, Markdown/HTML, citation markup, and raw source IDs in prose. It normalizes harmless whitespace but never rewrites claims, changes confidence, invents citations, drops invalid statements, or merges statements.

## Corrective retry

Exactly one corrective retry is allowed for schema, citation, atomicity, length, section, or safety validation failures. It receives the unchanged manifest and outer prompt plus one short allowlisted validator instruction. A V2 deterministic-key invariant failure is an internal application defect and never triggers provider retry. Raw output, exceptions, source text, prompts, and stack traces are never included in feedback. A second invalid result fails safely. There is no deterministic prose fallback.
