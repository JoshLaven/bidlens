# Compiler Specification

`OpportunityKnowledgeBriefCompiler` coordinates the deterministic evidence and model components for one already-created attempt. It has no HTTP or rendering behavior.

## Lifecycle stages

1. Authorize opportunity access and require the caller's `PURSUE` vote.
2. Create an append-only pending attempt and transition it to running.
3. Snapshot normalized current state.
4. Collect official evidence, communications, notes, and relevant history using the completed bounded collectors.
5. Deduplicate exact content, detect structured conflicts, apply the final total budget, and order sources.
6. Build, validate, version, canonically serialize, and SHA-256 hash the input manifest using the completed Session 4 services.
7. Make one structured model request, with at most one corrective validation retry inside the same attempt.
8. Validate schema, citation resolution, attribution, and safety.
9. Atomically save sources, statements, citation links, validated output, usage, and timings.

The source snapshot begins after the pending attempt transitions to running and completes after the bounded collectors finish. Manifest metadata is committed before the model call. No transaction remains open across source retrieval, parsing, the model request, or the corrective retry.

## Minimum evidence

V1 requires a meaningful non-placeholder title plus at least one meaningful contextual value: client, response deadline, source stage, solicitation number, or description. Placeholder titles such as `TBD`, `Unknown`, or `Untitled Opportunity` fail even if incidental database defaults exist. Failure is recorded as `insufficient_evidence`; the model is not called.

## Timings

Stage durations use monotonic clocks. `model_ms` is the Session 5 aggregate provider-call duration. `validation_ms` is the non-negative difference between the compiler's model/validation wall duration and that aggregate. `persistence_ms` covers application-side child construction and flush work up to immediately before the final commit; it intentionally does not claim database commit acknowledgement precision.

## Failure behavior

Failures use a stable category, stage, and safe message. Raw provider responses and stack traces are not persisted. A failed attempt is final; a user retry creates a new row.

Known failures preserve stable category, stage, message, retryability, model timing, and available usage. Unexpected failures use generic safe metadata. The compiler rolls back partial success children, finalizes an active attempt as failed in a separate transaction, and never modifies an earlier successful generation.
