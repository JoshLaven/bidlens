# Data Model

## Generation

`OpportunityKnowledgeBriefGeneration` records every Generate or Refresh attempt, including scope, lifecycle timestamps, contract versions, provider/model metadata, compact current-state snapshot, source summary, warnings, statistics, validated output, usage, timings, reproducibility, and safe failure metadata.

Completed attempts are append-only by service convention. PostgreSQL enforces one pending/running generation per organization/opportunity with a partial unique index.

Session 6 persists the exact compact current-state snapshot used by the compiler, manifest hash/version, source snapshot timestamps, source summaries, deterministic warnings, selected statistics, provider usage, and stage timings. The bounded description may be shortened; its current-state source row retains the selected-content hash, original/selected character counts, and truncation flag.

## Source

`OpportunityKnowledgeBriefSource` records source identity and provenance metadata. It deliberately stores no source body. `source_id` is unique within one generation.

Every controlled current-state field and every selected evidence source is inserted exactly once. Unavailable-source diagnostics are represented only in warnings/statistics and are never persisted as valid citable evidence.

## Statement

`OpportunityKnowledgeBriefStatement` stores atomic rendered claims. `statement_key` is unique within one generation.

## Citation link

`OpportunityKnowledgeBriefStatementSource` links statements to sources in citation order. A statement/source pair is unique.

Sources, statements, ordered citation links, validated `output_json`, and the succeeded transition are committed atomically.

## Scope and deletion

Generation rows reference organization, workspace, opportunity, and generating user. Child rows cascade when a generation is removed by test cleanup or a future retention process. Ordinary application behavior exposes no edit or delete operation.
