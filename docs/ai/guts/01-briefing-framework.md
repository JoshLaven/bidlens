# Briefing Framework

## Purpose

Produce a compact current-state briefing for an opportunity by separating deterministic evidence compilation from model summarization and final rendering.

## Trust order

1. Normalized current opportunity state
2. Official solicitation evidence
3. Attributed organizational knowledge
4. Historical context

Lower-ranked sources cannot silently override higher-ranked sources. Conflicts must remain visible and cited.

## Accuracy rules

- State only claims supported by included evidence.
- Do not infer causality, significance, consensus, ownership, deadlines, decisions, risk, or next steps.
- Treat notes and communications as attributed claims.
- Describe the present state; include history only when it explains a confirmed current fact or meaningful change.
- Never use previous generated briefings as evidence.

## Lifecycle

Generate and Refresh each create a new append-only attempt. Only one attempt may be pending or running for an organization/opportunity pair. A successful attempt becomes the latest readable result; a failed attempt leaves the prior success intact.
