# GUTS Presentation Contract V1

## Purpose

The purpose of Get Up To Speed (GUTS) is to quickly orient a user to the current state of an opportunity.

GUTS is not intended to replace source documents, emails, notes, or solicitation history.

Instead, it provides a concise, evidence-backed briefing that helps a user understand where an opportunity stands before exploring the underlying details.

The objective is not to answer every question.

The objective is to answer:

> What do I need to know before I dive in?

## Product Philosophy

GUTS is an orientation engine, not a decision engine.

It summarizes what BidLens knows.

It does not decide what users should do.

It does not evaluate strategy.

It does not infer organizational intent beyond what is directly supported by evidence.

It does not attempt to replace human judgment.

Every statement should ultimately be traceable to evidence already contained within BidLens.

## Design Principles

### Evidence First

Every displayed statement should be directly supported by existing evidence.

The UI should never generate new conclusions beyond those produced by the GUTS compiler.

### Concise by Default

Users should understand the opportunity in roughly 20–30 seconds.

This is an orientation surface, not a report.

Every section should prioritize brevity over completeness.

### Preserve Attribution

Internal discussions belong to people.

When organizational knowledge originates from notes or communications, preserve that attribution whenever practical.

Prefer:

- Josh recommended...
- Kendall suggested...

Avoid:

- The team plans...
- It was suggested...

unless multiple consistent sources genuinely support that framing.

### Evidence Before Inference

Prefer factual observations over inferred conclusions.

For example:

✅ Amendment 3 extended the response deadline.

❌ Timeline risk has increased.

The first is directly observable.

The second requires interpretation.

## Presentation Structure

### Overall Status

**Purpose:** Provide a concise orientation to the opportunity.

Examples:

- Opportunity type
- Current stage
- Response deadline
- Important identifying context

**Target:** Two concise bullets or approximately two short sentences.

### Recent Developments (Official Updates)

**Purpose:** Summarize meaningful external changes. This section summarizes changes made by the opportunity owner or sponsor, not by the user's organization.

Examples:

- Amendments
- Deadline extensions
- New solicitation documents
- Official updates
- Significant history events

Display newest developments first.

**Maximum:** Three concise bullets.

### Internal Activity

**Purpose:** Summarize meaningful organizational work.

Examples:

- Recommendations
- Discussions
- Previous experience
- Research findings
- Staffing suggestions
- Partner conversations

Maintain attribution whenever possible.

**Maximum:** Three concise bullets.

## Sections Explicitly Excluded from V1

### Risks

Not included.

**Reason:** Risk is an inference.

Identifying risk requires understanding organizational objectives, execution plans, resource constraints, and probability of failure.

These judgments extend beyond the evidence currently available to GUTS.

### Suggested Next Steps

Not included.

**Reason:** Recommendations are advisory.

BidLens is designed to summarize organizational knowledge rather than prescribe actions.

Users should retain ownership of strategic decisions.

Future versions may introduce recommendation-oriented experiences, but those are intentionally outside the scope of GUTS V1.

## Information Hierarchy

GUTS should answer:

- What is this opportunity?
- What has changed?
- What has my organization discussed?

GUTS should not attempt to answer:

- What should we do?
- What is our strategy?
- What is likely to happen?
- What is most risky?

Those questions belong to people.

## Relationship to the Rest of BidLens

GUTS is the entry point into the Opportunity Folder.

It is intentionally concise because the remainder of the workspace contains the supporting detail.

Users should naturally transition from GUTS into:

- Description
- Communications
- Notes
- Solicitation History
- Documents

GUTS should encourage exploration rather than replace it.

## Architectural Principle

The presentation layer remains separate from the compiler.

```text
Evidence
        ↓
GUTS Compiler
        ↓
Canonical GUTS Statements
        ↓
Presentation Layer
        ↓
Get Up To Speed
```

The presentation layer may:

- group
- rank
- order
- format

canonical GUTS statements.

It must not:

- generate new claims
- reinterpret evidence
- synthesize new conclusions
- create additional AI summaries

The compiler remains the single source of truth for synthesized content.

## Long-Term Vision

As BidLens accumulates organizational knowledge, future products may provide strategic insights, recommendations, or predictive analyses.

Those capabilities should remain distinct from GUTS.

GUTS should continue serving a single purpose:

Help users quickly understand the current state of an opportunity using trustworthy, evidence-backed organizational knowledge.
