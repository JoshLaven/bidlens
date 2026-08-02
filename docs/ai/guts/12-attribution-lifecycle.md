# GUTS Structured Attribution V2 — End-to-End Lifecycle

## Overview

This document is the canonical architectural reference for how one organizational statement moves through GUTS. It applies the [Organizational Knowledge Contract V2](10-organizational-knowledge-contract.md) and the [Structured Attribution V2 design](11-structured-attribution-design.md) across the complete pipeline. It describes the Phase B target explicitly; where the current V1 implementation differs, that difference is identified rather than treated as existing behavior.

The single example used throughout is an opportunity communication with database ID `6`:

> I recommend contacting Westat as a potential subcontractor for this opportunity.

The stored sender is Josh Laven (`josh@example.com`). At collection time that address uniquely matches BidLens user `4` in the opportunity workspace. The accepted canonical statement is:

> Josh Laven recommended contacting Westat as a potential subcontractor.

### Pipeline

```text
OpportunityCommunicationMessage 6
  "I recommend contacting Westat as a potential subcontractor..."
                         │
                         ▼
Evidence Collection     communication:6 + immutable author snapshot
                         │
                         ▼
Manifest                scoped, selected evidence + stable source ID
                         │
                         ▼
Prompt Assembly         bounded evidence + explicit citation/attribution contract
                         │
                         ▼
OpenAI Structured Output statement + source_ids + structured attribution
                         │
                         ▼
Compiler                canonical objects; no attribution inference or rewriting
                         │
                         ▼
Validator               structure, citations, actor/source match, fidelity
                         │
                         ▼
Persistence             append-only statement, attribution snapshot, source link
                         │
                         ▼
Presentation Mapper     ordering, date, citation metadata; canonical text unchanged
                         │
                         ▼
Opportunity Detail UI   Internal Activity statement + date + citation affordance
```

### Stage boundaries

| Stage | Purpose | Input | Output | Invariants | Trust boundary |
| --- | --- | --- | --- | --- | --- |
| Source material | Retain the original user-generated organizational record. | Authorized opportunity note or stored communication row. | Tenant-scoped source record. | The row ID and tenancy relationships identify the record; display names are not identity proof. | Database access establishes scope, not truth of the author's claim. |
| Evidence collection | Convert eligible records into canonical evidence. | Scoped records and author membership data. | `EvidenceSource` with stable ID, text, timestamp, and author snapshot. | No AI synthesis; deterministic normalization; unresolved people are not invented. | Collector code may establish source provenance and identity correspondence, but not whether the opinion is correct. |
| Manifest | Freeze the evidence selected for one generation. | Current state and collected evidence. | Versioned, hashable manifest. | Unique source IDs; organization/workspace/opportunity scope; immutable generation-time values. | A valid manifest proves which evidence was supplied, not that untrusted content is factual. |
| Prompt assembly | Give the model the evidence and exact output obligations. | Model-visible manifest, citation contract, output schema. | One bounded provider request. | Citable IDs are explicit; source authors remain associated with their evidence; untrusted text cannot redefine instructions. | The model receives private evidence but is never trusted to authorize, persist, or render. |
| Structured output | Return candidate synthesis in machine-readable form. | Prompted evidence and strict provider schema. | Candidate headline, statements, citations, confidence, and V2 attribution. | Attribution is required and nullable on every statement; citations remain separate. | Schema-valid provider output is still untrusted until application validation. |
| Compiler | Coordinate conversion into canonical application objects. | Provider output and the same manifest. | Candidate canonical briefing. | Preserve model text and declared metadata exactly; do not repair or infer identity. | Compiler orchestration does not confer semantic trust. |
| Validator | Decide whether the candidate obeys GUTS contracts. | Candidate briefing and manifest. | Validated briefing or safe failure. | Exact citations; actor/source correspondence; epistemic fidelity; strict official grounding. | This is the application trust gate for synthesized output. |
| Persistence | Durably record the validated generation. | Validated briefing, selected sources, citation links, generation metadata. | Append-only rows and canonical output. | Atomic write; exact validated attribution; failed output is not persisted as a successful briefing. | Persistence trusts only validator-approved canonical data. |
| Presentation mapper | Select and format canonical statements for the product contract. | Authorized persisted generation. | Presentation view models. | No new claims, rewriting, attribution reconstruction, or source reclassification. | The mapper may organize trusted data but may not synthesize it. |
| Opportunity Detail UI | Orient the authorized user and support evidence exploration. | Presentation view model. | Internal Activity content, date, and citation affordance. | Display canonical text only; keep source access tenant-scoped. | UI rendering cannot expand authorization or certify internal opinions. |

## Stage 1 — Source Material

### Opportunity communication

`OpportunityCommunicationMessage` retains an imported communication associated with a workspace and opportunity. Attribution-relevant fields currently include:

- `id`: the BidLens message record identifier; authoritative for addressing this stored row;
- `workspace_id` and `opportunity_id`: authoritative tenancy and opportunity scope for collection;
- `associated_user_id`: the user associated with the imported mailbox or workflow, not necessarily the message author;
- `sender_display_name`: provider-supplied display metadata;
- `sender_address`: provider-supplied sender address;
- `provider_timestamp`: the provider timestamp for the message;
- `provider`, mailbox/message/conversation identifiers, and `internet_message_id`: provider provenance and deduplication metadata;
- `body`: the retained source content used by the collector after existing selection and cleanup rules; and
- `created_at`, `updated_at`, and `imported_at`: BidLens record lifecycle timestamps.

The message row does not currently store an authoritative sender `user_id`. `associated_user_id` must not be repurposed as that identity. Phase B resolves a sender to an internal user only when the normalized sender email uniquely matches a member of the same workspace at collection time.

### Opportunity note

`OpportunityNote` retains a user-created note associated with an organization and opportunity. Attribution-relevant fields include:

- `id`: the BidLens note record identifier;
- `org_id` and `opportunity_id`: authoritative organization and opportunity scope;
- `user_id`: the durable internal author identity when the referenced user can be resolved;
- `body`: the organizational source content; and
- `created_at` and `updated_at`: the note timestamps.

The joined `User` provides the generation-time display name and email snapshot. Phase B must populate both snapshots. If the user cannot be resolved, placeholders such as `Unknown user` or `User 4` are not person identities; the source is represented as `internal_source` when otherwise eligible.

### Authority classification

| Value | What it establishes | What it does not establish |
| --- | --- | --- |
| Row ID and tenancy foreign keys | Which stored source belongs to which authorized opportunity context. | The objective truth of its content. |
| Note `user_id` | Durable internal author identity, subject to the joined source-author snapshot. | That the author's opinion is organizational consensus. |
| Unique workspace email match | Deterministic internal identity correspondence for a communication at collection time. | Global identity, mailbox ownership outside the workspace, or correctness of the message. |
| Sender/display name | A generation-time display snapshot. | Durable identity by itself. |
| Sender email | A provider-supplied address and, after normalization, a possible exact matching key. | A safe public field or proof beyond the scoped matching rule. |
| Provider/note timestamp | When the retained source says the communication or note occurred. | When every claim in the content became true. |
| Body text | What the source recorded. | Official fact, completed action, or consensus without supporting evidence. |

## Stage 2 — Evidence Collection

The organizational evidence collector queries only records inside the authorized organization/workspace/opportunity boundary, applies existing inclusion and content limits, and creates immutable `EvidenceSource` value objects. It does not summarize, interpret, or validate the author's opinion.

For the example, Phase B collection produces the conceptual value below. Field names use the existing `EvidenceSource.author` contract (`address` becomes `email` only in V2 statement attribution):

```json
{
  "source_id": "communication:6",
  "source_class": "organizational_knowledge",
  "source_type": "email",
  "authority": "attributed_claim",
  "citation_label": "Communication from Josh Laven",
  "text": "I recommend contacting Westat as a potential subcontractor for this opportunity.",
  "author": {
    "user_id": 4,
    "display_name": "Josh Laven",
    "address": "josh@example.com"
  },
  "occurred_at": "2026-07-31T16:30:00Z",
  "internal_model_name": "OpportunityCommunicationMessage",
  "internal_record_id": 6,
  "provider": "microsoft"
}
```

The example values are illustrative; the lifecycle rule does not depend on these particular IDs, address, provider, or date.

Collection invariants:

- `communication:6` is deterministic for this stored message and is unique within the generation.
- `source_class`, `source_type`, and `authority` are assigned by trusted application code, never by source text or the model.
- Source text remains untrusted data.
- Email and display-name normalization follows the exact V2 contract: Unicode NFKC, whitespace normalization, and case-folded comparison values.
- Internal communication identity resolution is workspace-scoped, email-based, and unique. Display name alone never resolves a user.
- The author snapshot is frozen in the evidence value used for this generation. It is not a live reference to future profile values.
- Missing or ambiguous identity produces an external actor snapshot or authorless `internal_source`; it never produces a guessed internal user.

Within a generation, the selected text, author snapshot, occurrence timestamp, source ID, and provenance fields are immutable inputs. A later generation may legitimately collect newer profile or source data and therefore produce a different manifest hash, but it cannot mutate the earlier generation.

## Stage 3 — Manifest

The manifest combines current opportunity state, selected evidence, exclusions/unavailable-source state, conflicts, and deterministic statistics into the complete input snapshot for one generation. The example `EvidenceSource` appears under selected organizational evidence with its author object intact.

Attribution-relevant manifest data for `communication:6` is:

- exact citable source ID: `communication:6`;
- source class: `organizational_knowledge`;
- source type: `email`;
- authority: `attributed_claim`;
- selected source text;
- author `user_id`, display-name snapshot, and address snapshot;
- occurrence timestamp; and
- internal/provenance metadata used by trusted application code.

Snapshots exist because provenance must describe the evidence as it was supplied to that generation. If Josh later changes his name or email, the old generation must continue to explain why its statement was attributed to the historical source-author snapshot. A refresh may record the new profile values in a new append-only generation; it never rewrites the previous one.

A valid manifest guarantees that:

- collection occurred inside one organization/workspace/opportunity scope;
- source IDs are deterministic and unique;
- selected evidence and author metadata passed contract validation;
- canonical serialization and hashing can detect any input change; and
- the compiler and validator can evaluate citations against the same frozen source inventory.

It does **not** guarantee that Josh's recommendation is correct, that Westat is available, or that contacting Westat is an approved organizational plan. Organizational content remains attributed, untrusted evidence.

## Stage 4 — Prompt Assembly

Prompt assembly transforms the manifest into a bounded model-visible representation and supplies the strict output schema. It does not create an additional summary or attribution inference.

For the example, the model is given enough structured context to know:

- the evidence identifier is exactly `communication:6`;
- the evidence is an organizational email;
- its author snapshot is internal user `4`, displayed as Josh Laven, with the normalized address needed by the attribution contract;
- the communication occurred on July 31, 2026; and
- Josh recommended contacting Westat as a potential subcontractor.

The citation contract separately identifies exactly which source IDs may appear in `source_ids`. The V2 attribution contract explains the required structured actor object and its relationship to confidence and evidence. Author metadata is already available before generation; the model is not expected to infer Josh's identity from prose alone.

Prompt assembly must preserve these separations:

- evidence text is untrusted data, not instructions;
- `source_ids` cite evidence, while `attribution` identifies actors;
- an author snapshot does not upgrade an opinion to fact or consensus; and
- provider output remains untrusted even when it satisfies the provider schema.

## Stage 5 — Structured Output

Phase B (`guts-output-v2`, prompt `guts-v9`) adds required nullable `attribution` to every output statement. A conceptual provider response containing the example is:

```json
{
  "headline": {
    "statement_key": "headline",
    "text": "The opportunity remains under active organizational review.",
    "importance": "high",
    "confidence": "supported",
    "source_ids": ["current_state:opportunity:180:source_stage"],
    "attribution": null
  },
  "summary_statements": [],
  "sections": [
    {
      "section_type": "organizational_knowledge",
      "statements": [
        {
          "statement_key": "organizational_knowledge-1",
          "text": "Josh Laven recommended contacting Westat as a potential subcontractor.",
          "importance": "high",
          "confidence": "attributed",
          "source_ids": ["communication:6"],
          "attribution": {
            "type": "person",
            "actors": [
              {
                "user_id": 4,
                "display_name": "Josh Laven",
                "email": "josh@example.com"
              }
            ]
          }
        }
      ]
    }
  ]
}
```

The headline illustrates that official/current-state statements use `attribution=null`; it is not derived from the example communication. The organizational statement independently carries its exact citation, attributed confidence, and structured actor snapshot.

Structured attribution is necessary because prose alone cannot safely answer whether a name is an actor, a subject, or an incidental reference. It also cannot reliably distinguish one speaker from team consensus. The machine-readable object lets the application validate actor cardinality, identity correspondence, confidence compatibility, and source support without extracting identity from sentence wording.

## Stage 6 — Compiler

The compiler coordinates provider invocation, conversion, validation, and persistence. For the example it converts the provider section entry into the canonical statement position and section metadata expected by the application.

The compiler preserves:

- statement key and placement;
- section type and position;
- exact statement text;
- importance and confidence;
- ordered source IDs; and
- the exact validated attribution object and actor order.

The compiler must never:

- infer attribution from names, pronouns, verbs, source authors, or citation labels;
- insert or remove an actor;
- resolve a model-supplied actor after generation;
- rewrite prose to make it appear attributed;
- upgrade a recommendation, concern, plan, question, or prediction into fact;
- repair a citation or silently add the author's source;
- collapse multiple actors into team consensus; or
- drop an invalid statement and mark the generation successful.

Normalization and identity resolution happen deterministically at evidence collection and contract-validation boundaries. The compiler is orchestration, not an alternate inference layer.

## Stage 7 — Validator

Validation uses the candidate output and the exact manifest supplied to the model. The provider schema is the first structural defense; the application validator remains the authoritative semantic and source-correspondence gate.

### Structural validation

The validator checks:

- the exact output object shape, required fields, enum values, and reserved statement keys;
- `attribution` is either `null` or the exact `person`/`internal_source` object;
- `person` contains 1–10 ordered, unique, valid actors;
- every actor contains all required keys and valid normalized snapshot combinations;
- `internal_source` contains `actors=[]`;
- `confidence="attributed"` has non-null attribution; and
- supported and authoritative uncertain statements have `attribution=null`.

### Semantic validation

The validator checks statement atomicity, prohibited unsupported claims, exact-date and exact-identifier grounding, known conflict handling, and whether epistemic status is preserved. Plans remain plans, recommendations remain recommendations, questions remain questions, and predictions remain predictions.

### Official and current-state validation

Official/current-state statements remain subject to strict evidence-class, field-specific citation, confidence, and factual grounding rules. Organizational evidence cannot substitute for the exact current deadline, solicitation number, or source-stage citation. Structured attribution does not relax any authoritative-evidence rule.

### Organizational knowledge validation

For the example the validator verifies that:

1. the statement is `confidence="attributed"` and has `type="person"`;
2. actor `user_id=4` exactly matches the author snapshot on cited source `communication:6`;
3. the actor's display name and email equal that frozen source snapshot;
4. `communication:6` is an eligible organizational source in this manifest;
5. the prose preserves Josh as the actor and the recommendation as a recommendation;
6. no completed contact, partnership, approval, or organizational decision was invented;
7. one person's statement was not upgraded to “the team” or “the organization”; and
8. no conflict was compressed into consensus.

For external actors, matching uses exact normalized email, or exact unambiguous source-local display name only when email is unavailable. Every named actor must match at least one cited eligible organizational source. `internal_source` is valid only for eligible authorless organizational evidence with explicit source framing; it cannot be used to hide a resolvable person.

The validator does **not** determine:

- whether Josh was correct;
- whether Westat is actually the best or an available partner;
- whether contacting Westat is strategically advisable;
- whether the recommendation will be adopted; or
- whether a concern, interpretation, or prediction is objectively justified or true.

It validates faithful provenance and epistemic status, not the objective correctness of an internal opinion.

## Stage 8 — Persistence

Only a fully validated successful generation becomes the latest successful briefing. The generation, selected sources, statements, and statement-source links are written atomically under the existing append-only lifecycle.

The example becomes a conceptual statement row:

```json
{
  "generation_id": 42,
  "statement_key": "organizational_knowledge-1",
  "placement_type": "section",
  "section_type": "organizational_knowledge",
  "position": 0,
  "text": "Josh Laven recommended contacting Westat as a potential subcontractor.",
  "importance": "high",
  "confidence": "attributed",
  "attribution_json": {
    "type": "person",
    "actors": [
      {
        "user_id": 4,
        "display_name": "Josh Laven",
        "email": "josh@example.com"
      }
    ]
  }
}
```

Its ordered statement-source link points to the same generation's persisted `OpportunityKnowledgeBriefSource` whose `source_id` is `communication:6`. That source row retains the generation-time author snapshots and occurrence timestamp needed for traceability. Attribution does not replace this citation link.

Persistence invariants:

- `attribution_json` stores the exact validated object; the repository does not reconstruct it.
- V2 non-attributed statements store SQL `NULL`.
- Every refresh creates a new generation; earlier successful or failed attempts are not rewritten.
- A failed refresh does not replace the latest successful briefing.
- Changing or deleting a user later does not rewrite the stored actor or source snapshots.
- Historical V1 statement rows remain valid with `attribution_json=NULL`; there is no backfill or regeneration.
- Generation version fields select the appropriate V1 or V2 compatibility path.

## Stage 9 — Presentation

The presentation mapper consumes an authorized persisted generation and applies the [Presentation Contract](09-presentation-contract.md). For Internal Activity it selects canonical organizational-knowledge statements, orders them according to the existing deterministic presentation rules, enforces the display budget, and derives the concise absolute date from the cited evidence timestamp.

For the example it carries forward:

- the exact canonical text;
- statement key and stable statement identity;
- importance and confidence;
- the structured actor metadata for future authorized use;
- citation metadata linked to `communication:6`; and
- `Jul 31`, derived from the source's persisted `occurred_at` value.

The mapper must not reconstruct attribution from prose or citation labels. It must not concatenate statements, rename actors, reinterpret the recommendation, synthesize a new status, or expose email snapshots by default. It renders trusted canonical output and presentation metadata only. Historical V1 statements continue to render from their canonical prose and citations without requiring structured attribution.

## Stage 10 — Opportunity Detail UI

The Get Up to Speed card's Internal Activity section ultimately displays:

```text
Josh Laven recommended contacting Westat as a potential subcontractor. · Jul 31
View source
```

The citation or future **View source** capability resolves through the persisted statement-source relationship and existing authorized opportunity/source access. It must not use an email address or provider URL as an unguarded direct link.

Users can trust this sentence in the precise GUTS sense:

- BidLens retained the source communication inside the opportunity's tenancy boundary;
- the selected evidence snapshot says Josh made the recommendation;
- the model returned Josh as a structured actor and cited that exact source;
- deterministic validation matched the actor to the cited evidence and preserved the recommendation's epistemic status;
- the compiler and presentation layer did not rewrite the accepted statement; and
- the stored citation can support an authorized path back to the evidence.

This trust does not mean BidLens certifies that Westat is the correct subcontractor. It means BidLens faithfully preserves who communicated what.

## Architectural Principles

- Official knowledge answers, “What does authoritative evidence establish as true?”
- Organizational knowledge answers, “Who communicated what?”
- GUTS preserves organizational memory; it does not certify internal opinions.
- Structured attribution is canonical for new V2 output and is preferred over prose inference.
- Validation checks structure and source correspondence before narrow language safeguards.
- Citations and attribution are complementary: citations identify evidence; attribution identifies actors.
- Author identity is resolved deterministically before model generation, never guessed afterward.
- Generation-time snapshots make historical briefings explainable and immutable.
- Organizational claims retain their epistemic status: plans remain plans, opinions remain opinions, and questions remain questions.
- One actor does not become team or organizational consensus.
- Official/current-state grounding remains strict and independent of attribution.
- The compiler, persistence layer, presentation mapper, and UI never create new claims.
- Actor metadata does not grant access; all source access remains tenancy- and opportunity-authorized.

## Phase B Traceability Gaps and Required Adjustments

Tracing the example through the current implementation exposes these bounded implementation gaps already anticipated by the Phase A design:

1. The communication collector snapshots sender display name and address but does not resolve a unique workspace member `user_id`.
2. The note collector snapshots `user_id` and a display value but does not populate the author's email in `EvidenceAuthor.address`.
3. Current collector fallbacks such as `Unknown sender`, `Unknown user`, or `User 4` must be prevented from becoming `person` actors.
4. Current provider and canonical statement contracts do not contain `attribution`.
5. Current statement persistence has no nullable `attribution_json` column.
6. Current validation relies partly on prose patterns and does not yet validate V2 actor/source correspondence.
7. Current presentation objects do not carry optional structured attribution.
8. The source contract calls the author email field `address`, while V2 output calls the actor field `email`; Phase B needs one explicit, deterministic mapping rather than parallel ad hoc normalization.

Before Phase B, implementation should use one shared normalization/correspondence module, complete source-author snapshots before manifest construction, switch behavior through the persisted output-schema version, validate structured attribution before prose safeguards, and persist only the exact validated object. These are implementation adjustments, not unresolved product decisions, and they do not require a manifest schema-version change.

## Design Checklist

- [ ] Every newly generated V2 statement has a required `attribution` field.
- [ ] Every attributed statement has valid structured `person` or `internal_source` attribution.
- [ ] Every named actor matches at least one cited eligible organizational evidence source.
- [ ] Internal actors carry complete immutable user ID, display-name, and email snapshots.
- [ ] External actors are not resolved to users by display name alone.
- [ ] Ambiguous or missing identities never become invented people.
- [ ] Every statement retains exact, independently validated source citations.
- [ ] Structured attribution never replaces citations.
- [ ] Official and current-state validation remains strict.
- [ ] Organizational validation prioritizes provenance and epistemic fidelity.
- [ ] No organizational or team consensus is invented.
- [ ] Conflicting sources are not compressed into consensus.
- [ ] Plans remain plans, recommendations remain recommendations, opinions remain opinions, and questions remain questions.
- [ ] Completed actions and objective facts are not manufactured from organizational discussion.
- [ ] Compiler and repository logic do not repair, infer, or rewrite attribution.
- [ ] Persistence is atomic and append-only.
- [ ] Historical V1 briefings remain valid without structured attribution.
- [ ] Presentation performs no reconstruction or reinterpretation.
- [ ] UI displays canonical statements only and keeps evidence access authorized.
- [ ] Email snapshots and source content remain absent from ordinary logs and default UI output.

