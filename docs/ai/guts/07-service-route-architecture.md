# Service and Route Architecture

## Session 1 persistence API

- `create_pending_generation`
- `mark_generation_running`
- `mark_generation_failed`
- `save_generation_success`
- `get_latest_successful_generation`
- `get_active_generation`
- `expire_stale_generation`
- `update_active_generation_metadata`

The repository validates organization/workspace/opportunity/user scope, lifecycle transitions, unique source and statement identifiers, and citation resolution. Child persistence and the success transition share one transaction.

## Application boundary

`OpportunityKnowledgeBriefService.generate` owns feature-flag enforcement, normal opportunity authorization, active organization/workspace resolution, personal Shortlist gating, active/stale attempt handling, pending-attempt creation, and safe compiler-error mapping.

`OpportunityKnowledgeBriefCompiler.generate` owns current state, all collectors, conflicts, selection, manifest/hash construction, the Session 5 model/validation coordinator, deterministic warnings/statistics, and persistence mapping. Routes remain future thin adapters and must not construct evidence or mutate completed attempts.

Authorized opportunity viewers may read the latest success. Generate/Refresh requires a personal `Vote.vote == "PURSUE"` for members and admins. Routes and UI remain intentionally absent through Session 6.

## Concurrency and transactions

- A fresh pending/running attempt returns `generation_already_in_progress`.
- An attempt whose `requested_at` is at or beyond the configured stale boundary is finalized with `stale_attempt`, then replaced.
- The partial unique index is the final race guard; insert races are mapped to the same safe in-progress error.
- Transaction A expires stale work and creates/commits the pending attempt.
- Short commits mark running and store bounded manifest metadata.
- Long retrieval/model work occurs without an application-held transaction.
- Transaction B atomically saves all successful children and finalizes success.
- Transaction C records safe failure metadata.
