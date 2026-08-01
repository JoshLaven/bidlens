# Get Up to Speed (GUTS)

GUTS is a shared, opportunity-level briefing that distills the current opportunity state, official evidence, and attributed organizational knowledge into a concise, citation-backed result. The UI label is **Get Up to Speed**; internal code and persistence may use **Opportunity Knowledge Brief**.

## Status

The design contract is locked. Sessions 1 through 6 are implemented. The application-facing service now coordinates shortlist-gated generation, stale-attempt recovery, the complete compiler pipeline, strict model validation, atomic source/statement/citation persistence, deterministic warnings/statistics, and safe failure finalization. HTTP routes and UI are not implemented.

## Reading order

1. [Briefing framework](01-briefing-framework.md)
2. [Input manifest](02-input-manifest.md)
3. [Source inclusion](03-source-inclusion.md)
4. [Output contract](04-output-contract.md)
5. [Compiler specification](05-compiler-specification.md)
6. [Data model](06-data-model.md)
7. [Service and route architecture](07-service-route-architecture.md)
8. [Implementation plan](08-implementation-plan.md)
9. [Presentation contract](09-presentation-contract.md)

## Locked product decisions

- Accuracy outranks completeness.
- Current state and official evidence outrank internal claims.
- Emails and notes remain attributed.
- Unsupported and causal inference are prohibited unless explicitly established.
- Previous AI output is never evidence.
- The briefing is shared at the opportunity level.
- Any authorized opportunity viewer may read the latest successful briefing.
- Members and admins must personally have `Vote.vote == "PURSUE"` to Generate or Refresh; there is no admin bypass.
- Every generation attempt is append-only, and Refresh creates a new generation.
- Structured JSON is canonical; statements are atomic and citation-backed with machine-readable source IDs.
- The model never controls final rendering.
- Generation is on demand, with one active attempt per opportunity.
- A failed refresh never replaces the latest successful briefing.

## Current phase

- Session 1 complete: documentation, configuration/constants, append-only persistence, and repository helpers.
- Session 2 complete: retained-source extraction cache, domain contracts, shortlist generation policy, and current-state assembly.
- Session 3 complete: deterministic internal-note and stored-communication evidence collectors.
- Session 4 complete: historical/official evidence, conservative cross-class deduplication, structured conflicts, final selection, and canonical manifest hashing.
- Session 5 complete: dedicated Responses API client, versioned prompt path, exact structured output, citation/safety validation, and one corrective retry.
- Session 6 complete: application service, compiler lifecycle, concurrency/stale handling, persistence coordination, warnings/statistics, and safe success/failure behavior.
- Next: route integration, Opportunity Folder UI/layout work, dogfood evaluation, and production timeout verification.

## Development dogfood command

The development/administrative CLI can generate a briefing through the normal application service without adding a web endpoint:

```bash
GUTS_ENABLED=true PYTHONPATH=src .venv/bin/python -m bidlens.cli generate-guts \
  --opportunity-id 554 \
  --user-id 1
```

Use `--organization-id` when the user belongs to more than one organization. The command preserves normal opportunity authorization and the caller's personal `PURSUE` requirement. It prints persisted briefing statements, safe operational metadata, and citation labels; it never prints source bodies, manifests, prompts, provider responses, storage keys, credentials, or private URLs.
