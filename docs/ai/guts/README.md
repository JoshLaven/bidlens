# Get Up to Speed (GUTS)

GUTS is a shared, opportunity-level briefing that distills the current opportunity state, official evidence, and attributed organizational knowledge into a concise, citation-backed result. The UI label is **Get Up to Speed**; internal code and persistence may use **Opportunity Knowledge Brief**.

## Status

The design contract is locked. Sessions 1 through 6 and Structured Attribution V2 Phase B are implemented. The application-facing service coordinates shortlist-gated generation, stale-attempt recovery, the complete compiler pipeline, strict model validation, atomic source/statement/citation/attribution persistence, deterministic warnings/statistics, and safe failure finalization.

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
10. [Organizational knowledge contract V2](10-organizational-knowledge-contract.md)
11. [Structured attribution V2 design](11-structured-attribution-design.md)
12. [Structured attribution V2 lifecycle](12-attribution-lifecycle.md)
13. [Prompt architecture](13-prompt-architecture.md)

## Locked product decisions

- Accuracy outranks completeness.
- Current state and official evidence outrank internal claims.
- Emails and notes remain attributed.
- Organizational knowledge validates faithful provenance, attribution, fidelity, and epistemic status; it does not certify that an attributed internal judgment is objectively correct.
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
- Structured Attribution V2 Phase B complete: `guts-v9`, `guts-output-v2`, immutable author snapshots, deterministic actor/source correspondence, nullable attribution persistence, and V1 read compatibility.
- Next: V2 dogfood evaluation and production verification.

## Development dogfood command

The development/administrative CLI can generate a briefing through the normal application service without adding a web endpoint:

```bash
GUTS_ENABLED=true PYTHONPATH=src .venv/bin/python -m bidlens.cli generate-guts \
  --opportunity-id 554 \
  --user-id 1
```

Use `--organization-id` when the user belongs to more than one organization. The command preserves normal opportunity authorization and the caller's personal `PURSUE` requirement. It prints persisted briefing statements, safe operational metadata, and citation labels; it never prints source bodies, manifests, prompts, provider responses, storage keys, credentials, or private URLs.

## Production Database Debugging

Local BidLens development uses SQLite by default. Production GUTS debugging must explicitly target the currently linked Railway PostgreSQL service; never assume a local shell, Codex session, or saved connection string points there. Railway's private and public hostnames can identify the same PostgreSQL database: deployed services normally use the private `DATABASE_URL`, while local Railway CLI commands need the current `DATABASE_PUBLIC_URL`.

Before creating or diagnosing a production generation, obtain variables from the currently linked Railway service and run the read-only preflight:

```bash
railway run -s Postgres sh -c \
  'DATABASE_URL="$DATABASE_PUBLIC_URL" PYTHONPATH=src .venv/bin/python -m bidlens.cli database-preflight'
```

Confirm the reported backend, host, database name, Alembic revision, and latest generation IDs match the intended environment. Then run `generate-guts` through the same Railway scope and `DATABASE_URL` assignment. Saved public connection strings, including `.env.railway.local`, may become stale after password rotation and must not be treated as the production source of truth.

Never paste database credentials or full connection URLs into documentation, logs, Codex prompts, or chat.
