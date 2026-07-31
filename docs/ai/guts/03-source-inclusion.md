# Source Inclusion

## Current state

Include compact normalized `Opportunity` fields representing current state. Snapshot them separately so current facts are auditable without retaining source bodies.

## Official evidence

Include retained source materials and retrievable official solicitation resources only when provenance and parsing status are known. Record provider, URL or internal record reference, filename, content hash, parser metadata, effective date, and whether BidLens retains the artifact.

Session 4 accepts only retained `rfp_document` PDF/DOCX materials. It verifies organization/workspace/opportunity scope and uses the Session 2 extraction cache. Email uploads and attachments, unresolved types, unsupported formats, missing objects, and parse failures never become evidence; eligible failures produce safe unavailable-source records. Retained evidence is `user_classified`, reproducible, and preferred over an external copy.

External evidence reuses Opportunity Brief's SAM.gov/Grants.gov discovery, download, timeout, byte/page/character, and parser limits. Only recognized SAM/Grants opportunity providers and HTTPS/HTTP `.gov` resources are accepted. External text is not archived or persisted on generation rows. Selected external evidence is `provider_retrieved`, `retained_by_bidlens=false`, and makes the selection partially reproducible. Exact content hash, provider resource ID, canonical URL, then filename/size are the conservative document identities; retained exact copies win.

Official V1 types are `solicitation_document`, deterministically named `amendment`, `official_source_description`, and `official_provider_record`. The collector defaults to five documents, 30,000 characters per document, and 60,000 total characters.

## Organizational knowledge

Include relevant stored messages and opportunity notes as attributed claims. Preserve author/sender and occurrence timestamps. Remove quoted email history, signatures, unsafe HTML, and duplicate messages before selection.

### Notes (Session 3)

Only current `OpportunityNote` rows in the requested organization/opportunity scope are eligible. Legacy `UserOpportunity.notes`, intake drafts, and AI output are excluded. Text is NFKC-normalized, stripped of invalid controls, whitespace-normalized, and filtered only when blank or one character long. Exact duplicate normalized notes are removed. Each selected note is capped at `GUTS_MAX_NOTE_CHARS` (3,000 by default); collection is capped at `GUTS_MAX_NOTES` (20) and `GUTS_MAX_TOTAL_NOTE_CHARS` (20,000).

If everything fits, notes remain in `created_at, id` order. When capped, selection first reserves the earliest note, then a recent-heavy half of the available slots, then fills remaining slots with deterministic evenly spaced chronological samples. Complete bounded note records must fit the total budget. Output is restored to chronological order.

Notes use `opportunity_note:{id}`, cite the resolved author (name, email, `User {id}`, or `Unknown user`) and note date, and remain `organizational_knowledge` / `attributed_claim`. Creation/update timestamps, internal IDs, exact selected-content hashes, and character counts are captured as provenance; note bodies are never logged.

### Stored communications (Session 3)

Only `OpportunityCommunicationMessage` rows in the verified organization/workspace/opportunity scope are eligible. `OpportunityConversation` supplies context metadata only and never becomes evidence by itself. Communication summaries, activity events, attachments, and raw provider payloads are excluded.

Bodies reuse Communication Summary's `clean_message_body()` for HTML-to-text conversion, script/style removal, quoted-reply and forwarded-header cutoff, mobile/footer cleanup, and common signature cutoff, followed by the same deterministic Unicode/control/whitespace normalization used for notes. Blank/quoted-only/signature-only bodies, a narrow exact acknowledgment allowlist, obvious automatic replies, delivery failures, and system/subscription notifications are excluded. Deduplication uses Internet Message ID, then provider message ID, then normalized sender/timestamp/subject/body hash; exact repeated cleaned bodies are also removed.

Each selected message is capped at `GUTS_MAX_MESSAGE_CHARS` (5,000 by default); collection is capped at `GUTS_MAX_MESSAGES` (50) and `GUTS_MAX_TOTAL_COMMUNICATION_CHARS` (60,000). Over-budget selection uses the same earliest/recent-heavy/evenly-spaced algorithm and restores chronological order. Messages use `communication:{id}`, cite sender display name or normalized address plus date, and remain attributed claims. Subject, provider, conversation, direction, timestamps, internal IDs, content hashes, and character counts are provenance rather than source-body additions.

Collector `available_count` is the number of scoped database records inspected; `selected_count` is the evidence returned, and omission diagnostics account for the difference. Collector defaults reserve at most 80,000 characters, below the 100,000-character overall manifest cap. A later final evidence-selection stage must apply that overall cap across current state and every source class.

## Historical context

Include source-change and solicitation-version events only when they clarify a current fact or important official update. Historical values never override normalized current state.

Session 4 maps material `OpportunityUpdateEvent` changes for response deadline, source stage, solicitation number, opportunity type, set-aside, materially changed title/agency, and meaningful NAICS fields. Each field becomes its own structured `opportunity_update:{event_id}:{field}` source. `OpportunityHistoryEvent` handlers accept only Grants synopsis/forecast versions and their known version, date, modification, changed-field, forecast-transition, and official-status fields. `source_updated` is omitted because its update-event row is canonical. Imports, Salesforce syncs, activity events, and unknown/raw event data are excluded.

History selection prioritizes stage/deadline transitions, then the newest material records, and restores chronological order. Defaults are 20 events and 10,000 characters.

## Cross-class selection and conflicts

Exact selected-content hashes and normalized text equality are deduplicated using precedence: current state, official evidence, organizational knowledge, historical context. Independently attributed organizational statements remain when attribution itself is meaningful; broad semantic similarity is intentionally absent.

Conflicts are detected only from structured values for deadline, solicitation number, agency/client, source stage, opportunity type, set-aside, and outcome. Equivalent dates, whitespace, and client capitalization do not conflict. Current normalized state wins internal/historical disagreement; newer dated official structured evidence wins older official evidence. Conflict IDs are SHA-256-derived, deterministic, material, and excluded from briefing by default. The model never resolves conflicts.

Final selection counts current state first, reserves one fitting note and one fitting communication when available, then allocates official, remaining organizational evidence round-robin, and historical evidence. Complete source boundaries are preserved. Lower-precedence evidence is omitted first when the 100,000-character final cap requires it. Collector omissions, final omissions, unavailable sources, truncation, latest-source time, statistics, and reproducibility are explicit diagnostics. Nominal collector maxima overlap by design; `EvidenceSelector` owns final enforcement.

## Exclusions

- Previous AI summaries or opportunity briefs
- Raw HTML when cleaned text exists
- Transport diagnostics, credentials, provider payloads, and stack traces
- Duplicate activity representations of stored messages
- Unparseable or provenance-ambiguous content presented as official fact
- Unpublished intake drafts
- Synchronized-message attachments, because their content is not retained locally
