# GUTS Structured Attribution V2 — Phase A Design Specification

## 1. Goals and Non-Goals

Structured attribution makes organizational provenance machine-verifiable while preserving the product boundary defined by the [GUTS Organizational Knowledge Contract V2](10-organizational-knowledge-contract.md).

### Goals

- Make attribution machine-verifiable.
- Preserve user-generated organizational knowledge faithfully.
- Reduce brittle prose validation.
- Retain exact evidence traceability.
- Preserve actor snapshots at generation time.
- Remain backward-compatible with successful V1 generations.

### Non-Goals

- Determine whether an internal opinion is objectively correct.
- Normalize every person into a BidLens user.
- Infer a missing actor.
- Infer organizational consensus.
- Remove or relax official and current-state factual validation.
- Backfill or regenerate historical generations.
- Add actor chips, drill-down UI, or user-facing attribution controls in Phase B.

## 2. Proposed Domain Contract

Every V2 model-output statement has an `attribution` field whose value is either `null` or this exact object:

```json
{
  "type": "person",
  "actors": [
    {
      "user_id": 4,
      "display_name": "Josh Laven",
      "email": "josh@joshlaven.com"
    }
  ]
}
```

### Attribution Object

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `type` | `"person" \| "internal_source"` | Yes | No other values. |
| `actors` | array of actor objects | Yes | `person`: 1–10 actors. `internal_source`: exactly empty. |

No additional properties are allowed.

### Actor Object

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `user_id` | positive integer or `null` | Yes | Durable identity for an internal BidLens user; `null` for an external person. |
| `display_name` | string or `null` | Yes | Immutable generation-time snapshot; normalized and limited to 200 characters. |
| `email` | string or `null` | Yes | Immutable normalized generation-time snapshot; limited to 320 characters. |

No additional properties are allowed. Strict provider output therefore always includes all three actor fields, using `null` where permitted.

Required combinations:

- Internal actor: `user_id`, `display_name`, and `email` are all non-null. If the current user profile has no name, the normalized email is also used as the display-name snapshot.
- External actor: `user_id` is `null`, and at least one of `display_name` or `email` is non-null.
- `person` attribution cannot contain an actor with both `display_name` and `email` null.
- `internal_source` attribution has `actors=[]`; it represents eligible organizational evidence with no resolvable person and never invents one.

Actor order is the order in which actors are represented in the canonical statement. The validator preserves this order. It does not alphabetize actors.

Duplicate actors are prohibited. Duplicate identity keys are:

1. the same non-null `user_id`;
2. otherwise, the same normalized non-null email; or
3. for source-local external attribution only, the same normalized display name.

Normalization:

- Apply Unicode NFKC normalization.
- Trim leading and trailing whitespace.
- Collapse internal whitespace in display names.
- Case-fold emails and display-name comparison values.
- Preserve the normalized display spelling in the snapshot; use case-folded values only for comparison.
- Do not remove email tags, rewrite aliases, infer domains, or merge provider-specific equivalent addresses.

Display name alone is not durable identity proof. It may establish only an exact, unambiguous correspondence to an author snapshot on a specifically cited source. Ambiguous display-name-only matches fail validation.

## 3. Provider Output Contract

V2 adds required `attribution` to every headline, summary, and section statement in the strict provider schema. The field is required structurally and accepts either `null` or the exact attribution object above.

- `confidence="attributed"` requires non-null `person` or `internal_source` attribution.
- `confidence="supported"` requires `attribution=null`.
- `confidence="uncertain"` requires `attribution=null` and authoritative uncertainty, a known conflict, or another existing non-organizational uncertainty basis. An uncertainty that only represents a person's question, concern, or uncertainty must use `confidence="attributed"` and preserve its epistemic status in prose.
- Current-state, official-evidence, and historical factual statements require `attribution=null`.
- Headlines and summary statements may be attributed when they meet the same organizational-source, attribution, and fidelity rules as section statements. Placement does not change the contract.
- `source_ids` remains a separate required array. Attribution does not replace citations and does not contain source IDs.
- Multiple actors use the ordered `actors` array. There is no `team` boolean or implicit consensus flag.

The application validator remains authoritative after provider-schema validation. It must reject structurally valid but source-inconsistent attribution.

## 4. Domain Validation Matrix

| Case | Result | Rationale |
| --- | --- | --- |
| `supported` + `attribution=null` + current/official source | Pass | Authoritative factual claim. |
| `supported` + non-null attribution | Fail | Supported facts cannot carry organizational attribution. |
| `attributed` + valid `person` attribution | Pass | Actor is structurally represented and matched to citations. |
| `attributed` + valid `internal_source` attribution | Pass | Eligible authorless organizational evidence supports explicit source framing. |
| `attributed` + `attribution=null` | Fail | Prose-only attribution is insufficient for V2 output. |
| `uncertain` + organizational source only + `attribution=null` | Fail | A person's uncertainty must remain an attributed organizational statement. |
| `uncertain` + authoritative uncertainty or known conflict + `attribution=null` | Pass | Evidence-backed unresolved factual state under existing uncertainty rules. |
| Current-state statement + non-null attribution | Fail | Authoritative current state is not organizational attribution. |
| Official-evidence statement + non-null attribution | Fail | Official fact is not organizational attribution. |
| Historical factual statement + non-null attribution | Fail | Historical fact remains non-attributed. |
| One actor + one matching cited source | Pass | Deterministic correspondence exists. |
| One actor + no matching cited source | Fail | Actor provenance is unsupported. |
| Multiple actors + matching source for each | Pass | Every actor has individual citation support. |
| Multiple actors + one actor without a match | Fail | Every named actor requires a matching cited source. |
| Team prose + one actor/source | Fail | One person cannot become team consensus. |
| Team prose + multiple consistent actors/sources | Pass | Coordinated wording has consistent multi-actor support. |
| Team prose + conflicting actors | Fail | Conflicts cannot become consensus. |
| External actor with email only | Pass | Email exactly matches a cited source-author snapshot. |
| External actor with display name only | Pass conditionally | Exact, unambiguous match to a cited source author; source-local correspondence only. |
| Internal actor with `user_id` and both snapshots | Pass | Durable ID and immutable display snapshots are present and match source metadata. |
| Internal actor missing either snapshot | Fail | V2 requires complete internal generation-time snapshots. |
| Duplicate actors | Fail | The ordered actor set must be unique. |
| Authorless eligible organizational source + `internal_source` | Pass | Explicit source framing avoids invented identity. |
| `internal_source` with non-empty actors | Fail | Attribution types cannot be mixed. |
| Additional supporting citations from unnamed actors | Pass conditionally | Allowed when each named actor has a match and prose does not imply endorsement or consensus by unnamed authors. |

## 5. Source-Author Matching Rules

Matching is limited to cited eligible organizational sources in the same generation and tenancy scope.

### Opportunity Notes

- `OpportunityNote.user_id` is the durable author identity.
- At collection time, snapshot the joined user's normalized display name and normalized email into `EvidenceAuthor`.
- An internal actor matches a note by exact `user_id`; its display-name and email snapshots must also equal the source-author snapshots generated for that source.
- Later user-profile changes do not alter the generation source or attribution snapshots.
- If the note's user record cannot be resolved, do not turn placeholders such as `Unknown user` or `User 4` into a person actor. Treat the eligible note as authorless and use `internal_source` when represented.

The current collector snapshots `user_id` and a display value but does not populate the user's email in `EvidenceAuthor.address`. Phase B must add that snapshot without changing note ownership.

### Opportunity Communication Messages

- The source author begins with the stored sender display name and sender address.
- Normalize the sender email as specified above.
- If the normalized sender email uniquely matches one workspace member's normalized user email at collection time, snapshot that member's `user_id`, display name, and email as the source author.
- Otherwise retain an external source author with `user_id=null` and available display/email snapshots.
- Never match a communication to an internal user by display name alone.
- If neither sender name nor address is available, do not turn `Unknown sender` into a person actor. Treat the eligible message as authorless and use `internal_source` when represented.

The current communication collector snapshots display name and address but does not resolve a matching internal `user_id`. Phase B must add deterministic workspace-scoped email resolution.

### Matching Precedence

For every actor, evaluate only cited source authors:

1. If `user_id` is non-null, require exact source-author `user_id`; then require both snapshots to match that source snapshot.
2. Otherwise, if email is non-null, require exact normalized-email correspondence.
3. Otherwise, require an exact normalized display-name match to exactly one distinct cited source-author identity.

Display name never overrides a conflicting user ID or email. Conflicting identity fields fail validation. Multiple cited sources with the same display name but different emails or user IDs are ambiguous and cannot support a name-only actor.

Every named actor must match at least one cited eligible source. One cited source may support only its own author identity. Additional organizational citations may remain unnamed when prose does not attribute their endorsement or imply consensus.

## 6. Persistence Design

Add nullable JSON column `attribution_json` to `opportunity_knowledge_brief_statements`.

- V2 attributed statements store the exact validated attribution object.
- V2 non-attributed statements store SQL `NULL`, corresponding to provider `attribution=null`.
- Historical V1 rows remain `NULL`; no backfill occurs.
- The repository accepts only the exact validated object shape and rejects unknown fields, invalid combinations, duplicate actors, or non-normalized snapshots.
- The validated canonical output and normalized statement row serialize the same attribution value.
- Attribution snapshots are immutable after persistence, consistent with append-only generation semantics.
- Deleting or changing a user does not rewrite historical attribution snapshots.

No database index is required in Phase B. V2 reads attribution through already-scoped generation/statement queries, and no product requirement searches statements by actor. Add an index only when an approved query requires it.

This design requires one additive migration. The column is nullable with no server default and no data migration.

## 7. Backward Compatibility

- V1 successful generations continue rendering their canonical prose, dates, and citations with `attribution_json=NULL`.
- V2 generations render the same canonical text and citations and additionally expose structured attribution to authorized application code.
- Presentation must support both forms. It must never reject or rewrite a historical V1 statement because attribution metadata is absent.
- Existing API responses remain unchanged unless a future authorized response explicitly adds an optional `attribution` property. Email snapshots are not exposed by default.
- No historical backfill or regeneration is required.
- Prompt and output-schema versions distinguish V1 and V2 generation contracts; persisted generation version fields select the compatibility path.
- V1 prose-only output remains valid only for historical persisted generations and explicit V1 test fixtures, not for newly generated V2 output.

## 8. Presentation Behavior

Internal Activity continues to:

- render canonical statement text unchanged;
- preserve its date and citations;
- retain structured actor metadata for future authorized evidence drill-down;
- avoid reconstructing, merging, or rewriting prose; and
- render historical V1 statements safely without structured attribution.

Phase B does not require visible actor chips or badges. Presentation may carry a sanitized attribution object internally, but email snapshots remain hidden unless a separately approved interaction requires them.

## 9. Security and Privacy

- Attribution and source-author data inherit existing organization, workspace, opportunity, and generation access controls.
- Actor metadata cannot authorize access and cannot weaken tenancy boundaries.
- Internal and external email snapshots are private metadata. Do not include them in ordinary logs, compiler timing logs, safe failures, or CLI diagnostics.
- Validation diagnostics may report controlled attribution type, actor count, correspondence rule, and opaque internal user IDs when appropriate, but not email addresses, source bodies, statement prose, or raw attribution JSON.
- API responses omit email snapshots by default. Any future exposure must be explicitly authorized, opportunity-scoped, and minimized for the UI need.
- Source drill-down must use existing authorized source references; attribution does not create a new direct source endpoint.
- Provider input may contain the bounded author snapshots required for generation, but they remain subject to the existing no-prompt/no-manifest logging rules.

## 10. Versioning Plan

Use these versions when Phase B ships:

- Prompt version: `guts-v9`
- Output-schema version: `guts-output-v2`
- Attribution contract version: `guts-attribution-v2`
- Manifest version: remain `guts-manifest-v1`

The prompt changes because the model must populate structured attribution. The output schema changes because every statement gains a required nullable field. `guts-attribution-v2` is the documented domain-contract identifier mapped deterministically to `guts-output-v2`; it is not a model-generated field and does not require a separate generation column.

The manifest schema already represents `EvidenceAuthor.user_id`, `display_name`, and `address`. Phase B populates those existing fields more completely but does not change manifest shape or hashing semantics. Therefore the manifest version does not change. Different author snapshots naturally produce a different manifest hash under existing canonical serialization.

## 11. Phase B Implementation Plan

Phase B is one bounded additive implementation:

1. Add a nullable `attribution_json` statement column and Alembic migration with no backfill or index.
2. Add exact attribution and actor domain contracts, normalization helpers, and the `guts-attribution-v2` constant.
3. Populate complete note-author snapshots and deterministically resolve communication senders to workspace users by normalized email.
4. Add required nullable `attribution` to provider statements and the validated canonical statement contract; bump prompt to `guts-v9` and output schema to `guts-output-v2`.
5. Update prompt instructions to require structured attribution for organizational claims and `null` elsewhere without changing official factual guidance.
6. Validate type/cardinality, confidence compatibility, actor uniqueness, actor/source correspondence, multi-actor support, authorless-source framing, conflict participation, and consensus scope.
7. Retain official/current-state grounding and narrow semantic-upgrade safeguards.
8. Persist exact validated attribution through repository rows and canonical output.
9. Extend presentation objects with optional structured attribution while preserving V1 fallback and unchanged displayed prose.
10. Add contract, schema, collector, validator, repository/migration, presentation, privacy, backward-compatibility, and end-to-end compiler tests.

Expected implementation surface:

- `src/bidlens/models.py` and one new Alembic revision for nullable `attribution_json`;
- GUTS configuration/constants, `contracts.py`, and `output_schema.py` for versioned attribution contracts;
- `organizational_evidence.py` for complete immutable author snapshots and workspace-scoped sender resolution;
- `prompt.py` and `model_client.py` for `guts-v9` structured output;
- `output_validation.py` for structural correspondence and retained semantic-upgrade safeguards;
- compiler serialization plus `repository.py` for exact canonical persistence;
- `presentation.py` for optional V2 metadata with V1 fallback; and
- focused configuration, contract, collector, Session 5 validator/model-client, persistence/migration, presentation, compiler/service, privacy, and full-regression tests.

Explicitly defer:

- removing all legacy prose checks;
- user training or feedback;
- source drill-down UI;
- historical backfill or regeneration;
- visible actor chips or badges;
- broad performance optimization; and
- unrelated prompt or presentation changes.

## 12. Phase B Acceptance Criteria

- Every newly generated `confidence="attributed"` statement has valid structured attribution.
- Every newly generated non-attributed statement has `attribution=null`.
- Every named actor deterministically corresponds to at least one cited eligible organizational source.
- Internal actors persist durable `user_id` and immutable normalized display/email snapshots.
- External actors remain external and do not require BidLens user creation.
- Multiple actors are ordered, unique, individually supported, and do not imply unsupported consensus.
- Authorless eligible sources use `internal_source` without invented people.
- Official/current-state factual validation is unchanged.
- Action-state, false-consensus, conflict, and objective-fact upgrade safeguards remain.
- Historical V1 generations remain readable without backfill or regeneration.
- The additive migration succeeds on supported databases and leaves historical rows null.
- No email, source content, prompt, manifest, raw attribution JSON, or provider output enters ordinary logs or safe failures.
- Existing authorization and tenancy tests remain green.
- Full test suite, compilation, single-head Alembic validation, migration upgrade validation, and `git diff --check` pass.
- Dogfood benchmark opportunities show fewer false-positive attribution failures without reduced official-fact validation.

## Resolved Architectural Decisions

The inspected architecture supports all supplied product decisions. No unresolved product decision blocks Phase B.

The source-author gaps are implementation work, not contract conflicts: note evidence must add the existing user email snapshot, and communication evidence must resolve internal senders by unique workspace-scoped normalized email. Display-name-only external attribution remains source-local and fails when ambiguous.
