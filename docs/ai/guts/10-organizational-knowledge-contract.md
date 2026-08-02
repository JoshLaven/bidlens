# GUTS Organizational Knowledge Contract V2

## Purpose

Organizational knowledge is a faithful compression of what people in the organization communicated or recorded in eligible BidLens notes and communications. It preserves useful organizational memory without certifying the underlying judgment as objective truth.

BidLens does not certify that an internal opinion, concern, recommendation, prediction, assessment, or plan is correct. For organizational knowledge, the factual claim being validated is that the attributed person or internal source communicated the represented idea.

This contract complements, and does not weaken, the strict factual-grounding contract for current state and official evidence.

## Trust Model

An organizational-knowledge statement is acceptable only when it satisfies all of the following requirements.

### 1. Provenance

Every statement cites one or more eligible notes or communications included in the generation manifest. Every citation resolves exactly to a persisted source. Official evidence or current state alone cannot support an organizational-knowledge statement.

### 2. Attribution

The statement preserves the relevant actor or uses explicit internal-source framing when a person cannot be represented reliably.

Prefer:

- Josh recommended considering Westat.
- Kendall raised a staffing concern.
- An internal note records prior work with this client.

Do not convert one person's statement into wording about "the team," "the organization," or organizational consensus.

### 3. Fidelity

The statement preserves the source's material meaning without inventing, materially altering, or upgrading it. Compression may remove incidental wording, but it must not change the represented idea, its actor, its object, or its qualifications.

### 4. Epistemic Status

- Recommendations remain recommendations.
- Concerns remain concerns.
- Opinions remain opinions.
- Assessments remain assessments.
- Plans remain plans.
- Questions remain questions.

An attributed opinion may be summarized faithfully even when BidLens cannot determine whether that opinion is objectively correct.

### 5. Consensus Discipline

One person's statement must not become "the team," "the organization," or organizational consensus. Team or coordinated wording requires multiple consistent cited sources. Conflicting sources must remain distinct and must not be compressed into consensus.

Multiple actors may share one statement only when the cited sources express substantially the same idea, the attribution remains accurate, and no material individual contribution is lost. Otherwise, use separate actor-attributed statements.

### 6. Action-State Discipline

- Proposed or planned actions must not become completed actions.
- Possible contributors must not become assigned owners.
- Potential partners must not become selected partners.
- Intended outreach must not become completed outreach.
- Discussion must not become a decision.

## What BidLens Does Not Validate

For a faithfully attributed organizational statement, BidLens does not attempt to determine whether:

- an internal opinion is objectively correct;
- a concern is justified;
- a recommendation is strategically wise;
- a predicted outcome is likely;
- an assessment of fit is accurate; or
- a proposed partner is truly the best choice.

These are human judgments. GUTS validates that the cited organizational evidence supports the attributed representation, not that the speaker's underlying view is true.

## Official Evidence Boundary

Current-state and official claims remain subject to strict factual grounding. This contract does not relax validation for:

- deadlines;
- amendments;
- solicitation identifiers;
- eligibility;
- funding;
- official requirements;
- current opportunity state; or
- other authoritative facts.

An internal statement about an official fact remains an attributed internal statement unless current-state or official evidence independently establishes that fact. Organizational evidence cannot override authoritative current state.

## Accepted and Rejected Examples

| Category | Source idea | Acceptable rendering | Unacceptable upgrade | Reason |
| --- | --- | --- | --- | --- |
| Opinion | "I think Westat would be a strong partner." | Josh viewed Westat as a potentially strong partner. | Westat is the best subcontractor. | Preserves an attributed opinion instead of asserting an objective ranking. |
| Concern | "I'm concerned we may not have enough recent adolescent-health references." | Josh expressed concern about whether the organization has enough recent adolescent-health references. | The organization lacks sufficient adolescent-health references. | Preserves uncertainty and attribution instead of converting concern into fact. |
| Recommendation | "We should contact Westat early." | Josh recommended contacting Westat early. | The organization will contact Westat early. | A recommendation is not an organizational commitment. |
| Plan | "I'll send the material to Cassie." | Josh planned to send the material to Cassie. | Josh sent the material to Cassie. | A planned action is not a completed action. |
| Prior experience | "I worked on a similar evaluation in 2022." | Kendall reported working on a similar evaluation in 2022. | The organization has proven experience with this exact requirement. | Keeps the claim personal and bounded. |
| Staffing suggestion | "Tom could help with the adolescent-health section." | Josh suggested Tom as a possible contributor to the adolescent-health section. | Tom is assigned to lead the adolescent-health section. | A possible contributor is not an assigned owner. |
| Possible partner | "ABC Services might be a good subcontractor." | Maria identified ABC Services as a possible subcontractor. | ABC Services is the selected subcontractor. | Possibility is not selection. |
| Internal research | "My notes show three relevant projects." | An internal note identifies three potentially relevant projects. | Official records confirm three qualifying projects. | Internal research is not official verification. |
| Unresolved question | "Do we have enough recent references?" | Josh asked whether the organization has enough recent references. | The organization does not have enough recent references. | A question does not establish its premise. |
| Multiple consistent actors | Josh and Tom separately recommend early partner outreach. | Josh and Tom recommended early partner outreach. | The organization decided to begin partner outreach. | Shared recommendation may be attributed jointly; it is still not a decision. |
| Multiple distinct actors | Josh raises a reference concern; Tom recommends highlighting prior projects. | Josh raised a reference concern. Tom recommended highlighting prior projects. | There is concern about references, but it was suggested that prior projects solve it. | Separate contributions preserve provenance and avoid implied consensus or causality. |
| Conflicting actors | Josh recommends Westat; Tom recommends a different partner. | Josh recommended Westat, while Tom recommended another partner. | The team recommends Westat. | Conflict must not be compressed into consensus. |

## Presentation Implications

Get Up To Speed presents organizational knowledge as concise, explicitly attributed organizational memory in **Internal Activity**. Information fidelity takes priority over elegant generalized prose.

The presentation layer may select, order, date, and format canonical statements. It must not remove attribution, upgrade epistemic status, merge actors, or synthesize a new organizational conclusion.

## Architectural Direction

The current output contract carries attribution through free-form statement text, confidence, and citations. The medium-term direction is to make attribution explicit in structured output metadata so validation can verify actor/source correspondence without relying primarily on an expanding verb vocabulary.

A future attributed statement could conceptually carry metadata such as:

```json
{
  "text": "Josh recommended considering Westat.",
  "confidence": "attributed",
  "source_ids": ["communication:6"],
  "attribution": {
    "type": "person",
    "actor": "Josh Laven"
  }
}
```

The exact Phase A schema, person and internal-source rules, source-author correspondence, persistence design, and compatibility behavior are locked in [Structured Attribution V2 Design](11-structured-attribution-design.md).

The architectural goal is to:

- validate actor/source correspondence structurally;
- reduce dependence on growing verb lists and prose regular expressions; and
- retain deterministic prose safeguards for false consensus, completed-action upgrades, selected-role or partner upgrades, and conversion of attributed views into objective facts.

## Current Implementation Audit

### Rules Already Aligned

- Organizational statements require organizational citations and `attributed` confidence.
- Every cited source ID must resolve exactly to selected evidence.
- Section/source compatibility separates organizational knowledge from current and official evidence.
- Team or coordinated wording requires multiple citations and rejects known conflicts.
- Deterministic objective-upgrade patterns reject selected roles, completed actions, and definitive organizational claims.
- Current deadlines, solicitation numbers, source stage, exact dates, official facts, and current state retain stricter grounding rules.
- Persistence keeps statement-to-source links and source author metadata, and presentation preserves canonical text, confidence, citations, and dates without rewriting.

### Gaps and Over-Strict Rules

- `output_validation.py` infers attribution from controlled verb lists and name-shaped regular expressions. Faithful prose can fail when an ordinary evaluative verb is absent.
- Actor detection proves only that prose looks attributed; it does not verify that the named actor corresponds to the author metadata of the cited note or communication.
- Fidelity and epistemic status are partly approximated through prohibited phrases and objective-upgrade regular expressions. They cannot comprehensively establish semantic faithfulness.
- The current output schema has no explicit actor, source-frame, or attribution-type field.
- The statement persistence model has no structured attribution field. Source author data exists on cited source rows, but the model-selected attribution framing is not stored separately.
- Historical successful generations contain attributed prose and citation links but no structured attribution metadata.

### Rules That Should Remain Unchanged

- Exact citation resolution and citation integrity.
- Current-state and official-evidence grounding.
- Exact current deadline, solicitation identifier, source-stage, and supported date checks.
- Confidence/source and section/source compatibility.
- False-consensus and known-conflict safeguards.
- Action-state and objective-fact upgrade rejection.
- Append-only persistence, one corrective retry, safe failures, and authorization.

## Compatibility and Persistence

Structured attribution can be introduced additively at the domain and provider-output layers, with a new output-schema version and prompt version. Historical successful generations must remain readable without regeneration.

Persisting structured attribution as canonical statement metadata requires a deliberate persistence choice:

1. **Recommended:** add a nullable `attribution_json` field to persisted statements. New generations populate it; historical generations remain `NULL` and continue rendering from their existing text and citations.
2. Derive attribution only from cited source-author rows. This avoids a migration but cannot faithfully represent model-selected internal-source framing, multiple actors, or a subset of actors across several citations.
3. Store attribution only inside generation `output_json`. This avoids a new column but conflicts with the current normalized statement/presentation path and would create two canonical read paths.

The recommended nullable field requires a small migration but is additive and backward-compatible. No historical rows need backfilling. Readers should treat absent attribution metadata as V1 and preserve existing canonical prose.

## Recommended Implementation Plan

### Phase A — Contract and Compatibility Design

- Finalize attribution types: person, multiple people, and explicit internal-source framing.
- Define whether actor identity uses a source author identifier, a display-name snapshot, or both.
- Define correspondence rules when one statement cites multiple sources or sources without known authors.
- Define nullable V1 compatibility and prompt/output-schema versioning.
- Specify the persistence representation and presentation fallback.

### Phase B — Additive Schema and Validator Implementation

- Add structured attribution metadata to new model-output and validated-statement contracts.
- Add it to the strict provider schema and version the prompt/output schema.
- Add nullable persistence and repository support if the recommended canonical-storage option is selected.
- Verify person attribution against cited note/communication author metadata.
- Require explicit internal-source framing for eligible authorless sources.
- Keep current-state and official-evidence validation unchanged.
- Continue persisting citations and canonical prose exactly as validated.

### Phase C — Reduce Brittle Prose Validation

- Narrow verb-list and actor-name regex checks made redundant by verified attribution metadata.
- Retain deterministic safeguards against false consensus, conflicting-source consensus, completed-action upgrades, assigned-owner or selected-partner upgrades, and objective-fact conversion.
- Keep V1 fallback validation for historical or compatibility fixtures where structured attribution is absent.

### Phase D — Dogfood and Compare

- Run the same benchmark opportunities across the currently supported models.
- Compare validation pass rate, actor/source correspondence, epistemic-status fidelity, and briefing quality.
- Inspect disagreements without weakening official factual grounding.
- Decide when structured attribution becomes required for all new generations.

## Recommended Next Codex Session

Phase A is complete in [Structured Attribution V2 Design](11-structured-attribution-design.md). The next implementation session is the bounded Phase B scope defined there: additive persistence, contracts/schema, prompt, source-author snapshots, structural validation, repository wiring, presentation compatibility, and tests.

Defer removal or narrowing of existing prose checks to Phase C after structured validation has dogfood evidence.

## Resolved Product Decisions

Phase A resolves actor identity snapshots, external participants, ordered multiple actors, `internal_source`, required nullable provider attribution, nullable JSON persistence, actor/source correspondence, historical compatibility, and V2 enforcement. No unresolved product decision blocks Phase B.
