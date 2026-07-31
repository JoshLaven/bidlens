# Implementation Plan

## Session 1 — complete

- Lock design documentation
- Add dedicated configuration and string constants
- Add four append-only persistence models and migration
- Add lifecycle/query repository helpers and focused tests

## Session 2 — complete

- Added retained `OpportunitySourceMaterial` extraction-cache persistence.
- Reused bounded PDF/DOCX parsing and private storage abstractions.
- Added strict database-free domain contracts and stable current-state source IDs.
- Added shared view/generation access policy with mandatory personal `PURSUE` for members and admins.
- Added deterministic current-state assembly for opportunity facts, current outcome, current team interest, and minimal Salesforce linkage.
- Model invocation remains intentionally absent.

## Session 3 — complete

- Added an immutable collector-result contract and richer internal source provenance fields.
- Added scoped `OpportunityNote` collection with normalization, conservative meaningful-content filtering, attribution, stable citations/hashes, and deterministic bounded selection.
- Added organization/workspace/opportunity-scoped stored-message collection with shared Communication Summary cleaning, reliable ID deduplication, deterministic fallback/body deduplication, narrow automated-message filtering, attribution, and bounded chronological selection.
- Added dedicated note/message count, per-record, and total-character settings whose combined defaults remain below the overall GUTS input cap.
- Added focused collector, contract, configuration, injection-boundary, no-body-logging, and Communication Summary regression coverage.
- No compiler, manifest, model invocation, route, or UI was added.

## Session 4 — complete

- Added allowlisted structured history collection from update and Grants version events.
- Added retained official evidence through the extraction cache and external SAM/Grants evidence through existing Opportunity Brief acquisition limits.
- Added safe unavailable-source results, retention/reproducibility metadata, canonical URLs, and retained-copy preference.
- Added conservative cross-class exact deduplication and structured deterministic conflict records.
- Added final total-budget selection with balanced note/message opportunity and class precedence.
- Added manifest construction/validation, volatile-metadata-independent canonical UTF-8 serialization, and SHA-256 hashing.
- Model calls, prompt/output validation, orchestration, persistence coordination, routes, and UI remain absent.

## Session 5 — complete

- Added a dedicated OpenAI Responses API model client with configured model, timeout, SDK retry count, output-token cap, temperature zero, strict JSON Schema, usage capture, and safe latency instrumentation.
- Added a stable versioned outer prompt; the manifest remains separately serialized untrusted evidence data.
- Added the exact provider-output contracts and schema without application-owned display fields.
- Added deterministic structure, citation, confidence, section, attribution, atomicity, prohibited-language, date, identifier, and output-size validation.
- Added exactly one safe corrective validation retry and usage/timing aggregation across both calls.
- Added safe configuration, timeout, provider, schema, citation, and unsafe-output errors with no local prose fallback.
- Compiler lifecycle, attempt persistence, concurrency/stale recovery, routes, UI, and dogfood evaluation remain absent.

## Session 6 — complete

- Added `OpportunityKnowledgeBriefService` with feature, authorization, organization, workspace, and personal `PURSUE` enforcement.
- Added active-attempt prechecks, exact stale-boundary expiry, and safe partial-index race mapping.
- Added the complete compiler lifecycle across current state, all collectors, conflict detection, final selection, manifest hashing, model validation/retry, and persistence mapping.
- Added deterministic minimum-evidence rejection, warnings, statistics, per-class source summaries, reproducibility metadata, usage, and monotonic stage timings.
- Added exact current-state and selected-evidence source rows, ordered statements/citations, validated output, and compact snapshot persistence.
- Added rollback-safe atomic success and separate safe failure finalization without replacing prior successes.
- HTTP routes, Opportunity Folder UI/layout changes, automatic generation, background work, telemetry, and dogfood evaluation remain absent.

## Later sessions

1. Thin route integration and latest-success read behavior
2. Opportunity Folder read/generate UI with application-controlled rendering
3. Opportunity Folder layout integration
4. Dogfood evaluation, production timeout verification, privacy review, and end-to-end verification

Each phase must leave prior successful generations readable and must not use previous AI output as evidence.
