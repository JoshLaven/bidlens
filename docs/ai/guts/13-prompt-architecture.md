# GUTS Prompt Architecture

## Purpose

GUTS prompt versions are executable configuration, not metadata labels. A configured version must resolve to one explicit registered prompt definition before a generation attempt can proceed. This keeps persisted `prompt_version` metadata structurally tied to the static instructions actually sent to the provider.

## Authoritative static instructions

The prompt registry and authoritative static instructions live in:

```text
src/bidlens/services/opportunity_knowledge_brief/prompt.py
```

The current registry contains two explicit definitions:

```text
guts-v8 → build_guts_v8_prompt → GUTS_V8_SYSTEM_INSTRUCTIONS
guts-v9 → build_guts_v9_prompt → GUTS_V9_SYSTEM_INSTRUCTIONS
```

The `guts-v8` instruction value remains unchanged and is paired with `guts-output-v1`.
`guts-v9` extends that immutable V1 wording with the Structured Attribution V2 contract and is paired with `guts-output-v2`.

`GUTS_PROMPT_VERSION` selects a registry entry. An unknown value raises a controlled, content-free configuration error before a pending generation is created. GUTS never falls back to the newest or only registered prompt.

## Request composition

The effective model request is composed from distinct trusted and untrusted inputs:

```text
GUTS_PROMPT_VERSION
        │
        ▼
Prompt registry ───────────────► selected static instructions
                                      │
Runtime current state                 │
Selected evidence                     │
Known conflicts                       ├──► OpenAI Responses request
Generation constraints                │
Citation contract                     │
        │                              │
        └──► model-visible manifest ───┘

Strict output schema ─────────────────► structured-output constraint
Model capability/configuration ───────► request parameters
Optional safe validation feedback ────► corrective retry input only
```

`manifest_input()` serializes the selected prompt version, citation contract, optional corrective feedback, and model-visible manifest into compact JSON. Free text within the manifest is explicitly treated as untrusted evidence, not outer instructions.

The model-visible manifest contains the runtime opportunity state and selected source content required to produce a briefing. Internal-only fields such as citation labels, content hashes, internal record identifiers, provenance, conflict IDs, and parser metadata are removed from provider input by the existing prompt boundary.

## Corrective retry

One corrective retry is permitted under the existing GUTS validation contract. A `GUTSModelClient` resolves its prompt definition once during construction and reuses that same immutable definition for both the initial call and corrective retry. A configuration change between calls cannot cause one generation to span two prompt versions.

The retry uses the same:

- selected static instructions;
- prompt version;
- manifest evidence;
- citation inventory;
- strict output schema; and
- model configuration.

Only bounded, allowlisted validation feedback is added to the runtime JSON input.

## What the prompt version controls

The configured prompt version controls:

- which registered static instruction definition is selected;
- the `prompt_version` value placed in runtime input;
- OpenAI request metadata; and
- the version persisted on the generation attempt.

The registered definition also declares its compatible output-schema version so invalid prompt/schema pairings fail before generation. The version does not select or modify:

- the manifest schema version;
- evidence collection or selection;
- output-schema implementation details beyond that compatibility pairing;
- compiler or validator behavior;
- provider model capabilities;
- presentation mapping; or
- UI rendering.

Those components retain their own explicit contracts and versions.

## Provider request elements that are not prompt prose

Several provider-request fields influence generation but are not part of the static prompt text:

- the strict JSON output schema from `output_schema.py`;
- exact `allowed_source_ids` and required current-state citation IDs;
- runtime evidence serialized from the manifest;
- the configured provider model;
- output-token limits;
- model-specific request capabilities such as temperature support;
- manifest/output-schema versions in request metadata; and
- sanitized validation feedback on the one corrective retry.

The Markdown contracts under `docs/ai/guts/` are architectural and product documentation. They are not loaded into the runtime prompt.

## Reproducibility and comparison

An authoritative registry prevents an arbitrary metadata label from describing unrelated instruction text. A persisted successful generation records the registered prompt version, manifest version and hash, output-schema version, provider, and model. Together, those values establish which application contracts and evidence snapshot produced the result.

Adding a future prompt requires an explicit registry entry and distinct immutable instruction definition. Existing entries remain available for controlled comparison and historical interpretation; they are not silently redirected to newer wording.

Synthetic V1 compatibility fixtures freeze representative `guts-v8`/`guts-output-v1` persisted behavior. They ensure later output-contract work does not require historical attribution backfills, rewrite canonical prose, detach citations, reorder statements, or infer actor metadata while reading older generations.
