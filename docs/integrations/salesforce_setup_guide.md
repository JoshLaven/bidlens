# BidLens Salesforce V1 Setup Guide

This guide helps a customer Salesforce Administrator and a BidLens Workspace
Admin configure Salesforce, connect it to BidLens, and verify the first
successful Opportunity sync.

BidLens V1 uses Salesforce as an outbound CRM destination. The implementation is
workspace-scoped: each BidLens workspace connects to one Salesforce organization
through its own OAuth authorization.

## 1. Overview

### What BidLens sends to Salesforce

BidLens can create or link Salesforce Opportunity records when eligible BidLens
opportunities are marked Interested or promoted by an admin.

In V1, BidLens can:

1. Connect a BidLens workspace to Salesforce using OAuth.
2. Store the Salesforce connection securely for that workspace.
3. Create a Salesforce Opportunity from a qualified BidLens opportunity.
4. Link a BidLens opportunity to an existing Salesforce Opportunity by external source ID.
5. Update Salesforce intake status when an opportunity is linked or promoted.
6. Push selected later source updates to Salesforce for linked opportunities.
7. Show the Salesforce Opportunity ID, URL, connection status, and sync history in BidLens.

### What BidLens does not do in V1

BidLens V1 does not provide:

- General bidirectional synchronization.
- Pulling Salesforce Opportunities back into BidLens.
- Configurable field mapping.
- Configurable default Opportunity Owner.
- Configurable default Record Type.
- Configurable sync direction.
- Configurable automatic push rules.
- Automatic retry queue or reconciliation for failed Salesforce syncs.
- More than one Salesforce organization per BidLens workspace.

### Roles involved

Required customer roles:

- Salesforce Administrator: creates/configures the Salesforce Connected App, schema, permissions, and access.
- BidLens Workspace Admin: connects Salesforce inside BidLens and verifies connection health.

BidLens operator role:

- BidLens deployer/operator: configures Salesforce environment variables in the BidLens deployment environment.

## 2. Prerequisites

### Required BidLens role

The person connecting Salesforce in BidLens must be a Workspace Admin.

Members cannot connect or disconnect Salesforce.

### Required Salesforce API access

BidLens uses Salesforce REST API endpoints. The Salesforce organization and
authorizing user must have API access.

Salesforce API availability depends on the customer’s Salesforce edition and
licenses. Salesforce documents API access as included by default in Enterprise,
Unlimited, Performance, and Developer editions. Professional Edition requires
API access as an add-on. Group and Essentials editions do not include API access.
See Salesforce Help: [Salesforce editions with API access](https://help.salesforce.com/s/articleView?id=000385436&language=en_US&type=1).

Required Salesforce user capability:

- API Enabled.
- Access to the BidLens Connected App.
- Read, create, and update access to Opportunity.
- Field-level access to all fields listed in this guide.

### Information that must already exist

Before connecting BidLens, confirm:

- BidLens has provisioned the customer workspace.
- The BidLens deployment has a public HTTPS URL for hosted use.
- The Salesforce Connected App exists and has OAuth enabled.
- The Connected App callback URL exactly matches the BidLens callback URL.
- Required Opportunity fields and picklist values exist in Salesforce.
- The Salesforce user who authorizes BidLens has Connected App access.

## 3. Salesforce Connected App setup

These steps are performed by the Salesforce Administrator.

### Values supplied by BidLens

BidLens or the BidLens operator supplies:

- BidLens public application URL.
- `SALESFORCE_REDIRECT_URI`.
- Guidance for required OAuth scopes.

### Values created in Salesforce

Salesforce creates:

- Consumer Key.
- Consumer Secret.
- Connected App access policies.

The Consumer Key becomes `SALESFORCE_CLIENT_ID` in BidLens. The Consumer Secret
becomes `SALESFORCE_CLIENT_SECRET` in BidLens.

### Create or edit the Connected App

1. In Salesforce, open Setup.
2. In Quick Find, search for `App Manager`.
3. Open App Manager.
4. Click New Connected App, or edit the existing BidLens Connected App.
5. Enter a clear app name, for example `BidLens`.
6. Enter a contact email controlled by the customer or Salesforce admin team.
7. In API / OAuth settings, select Enable OAuth Settings.

### Configure the callback URL

Set the Callback URL to the exact BidLens redirect URI.

Hosted production/staging format:

```text
https://<bidlens-domain>/api/salesforce/oauth/callback
```

Local development format:

```text
http://127.0.0.1:8000/api/salesforce/oauth/callback
```

The callback URL in Salesforce must exactly match `SALESFORCE_REDIRECT_URI` in
the BidLens deployment. A mismatch will cause OAuth authorization to fail.

### Configure OAuth scopes

Add these OAuth scopes:

- Manage user data via APIs (`api`)
- Perform requests at any time (`refresh_token`, `offline_access`)

BidLens currently requests:

```text
api refresh_token
```

Salesforce describes OAuth scopes as the token-level permissions granted to a
Connected App. See Salesforce Help: [Select OAuth scopes for a Connected App](https://help.salesforce.com/s/articleView?id=xcloud.shr_api_enable_oauth_settings_select_the_oauth_scopes_to_apply_to_the_connected_app.htm&language=en_US&type=5).

### PKCE requirements

BidLens uses the OAuth Authorization Code flow with PKCE using the S256 code
challenge method.

Required:

- Do not configure the Connected App in a way that blocks Authorization Code flow.
- Do not require a separate OAuth flow that omits the confidential app consumer secret.
- Keep the app configured as a confidential server-side integration with a Consumer Secret.

BidLens sends both the configured client secret and the PKCE code verifier during
the token exchange.

### Refresh-token policy

Required:

- Allow refresh tokens for the Connected App.

Recommended for beta:

- Use a refresh-token policy that does not expire the refresh token immediately
  after the browser session ends.
- If your Salesforce security policy requires refresh token expiration, make
  sure Workspace Admins know they may need to use Reconnect / Reauthorize in
  BidLens.

### Permitted-user policy

Recommended:

- Set Permitted Users to Admin approved users are pre-authorized.
- Grant access through a Permission Set assigned only to the dedicated
  integration user or approved Salesforce admins.

Alternative:

- All users may self-authorize is simpler, but less controlled.

Salesforce documents that Admin approved users are pre-authorized requires access
through a Profile or Permission Set. See Salesforce Help:
[Not Approved for Access error](https://help.salesforce.com/s/articleView?id=000212208&language=en_US&type=1).

### Grant users access to the Connected App

If using Admin approved users are pre-authorized:

1. In Salesforce Setup, open the Connected App management page.
2. Locate the BidLens Connected App.
3. Grant access using a Permission Set or Profile.
4. Assign that Permission Set to the user who will authorize BidLens.

Recommended:

- Use a dedicated Salesforce integration user.
- Assign a focused BidLens integration permission set to that user.

Hard requirement:

- The Salesforce user who authorizes BidLens must be able to authorize the
  Connected App and access the required Opportunity object and fields.

## 4. Required Salesforce schema

BidLens expects the following Opportunity fields.

Important: The current implementation expects the exact API names shown below.
Do not rename them in Salesforce without changing BidLens code.

| Label | API name | Standard/custom | Recommended type | Suggested length | BidLens usage | Picklist values | External ID | Uniqueness |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Opportunity Name | `Name` | Standard | Standard Opportunity Name | Salesforce standard | Writes on create and source update; reads during duplicate lookup response | Not applicable | No | Not applicable |
| Stage | `StageName` | Standard | Standard picklist | Salesforce standard | Writes on create; reads picklist values during validation | Must include `Prospecting` | No | Not applicable |
| Close Date | `CloseDate` | Standard | Standard date | Salesforce standard | Writes on create and source update | Not applicable | No | Not applicable |
| Description | `Description` | Standard | Standard long text area | Salesforce standard | Writes on create and source update | Not applicable | No | Not applicable |
| External Source ID | `External_Source_ID__c` | Custom | Text | At least 255 recommended | Writes on create; reads during duplicate lookup | Not applicable | Recommended | Recommended, not required by BidLens |
| Intake Status | `Intake_Status__c` | Custom | Picklist | Salesforce picklist | Writes on create and promotion; reads during duplicate lookup response | Must accept `Prospect_Feed` | No | Not applicable |
| Intake Source | `Intake_Source__c` | Custom | Picklist | Salesforce picklist | Reads active picklist values; writes the opportunity's originating source on create | Must include `SAM`, `Grants.gov`, and `GovWin` | No | Not applicable |

### Duplicate matching behavior

BidLens currently searches Salesforce with:

```text
External_Source_ID__c = <BidLens source_record_id>
```

BidLens does not require Salesforce uniqueness enforcement, but uniqueness is
strongly recommended to prevent duplicate or colliding matches.

## 5. Minimum permissions checklist

Required for the authorizing Salesforce user:

- API Enabled.
- Connected App access.
- Opportunity Read.
- Opportunity Create.
- Opportunity Edit/Update.
- Field-level read access:
  - `Name`
  - `StageName`
  - `CloseDate`
  - `Description`
  - `External_Source_ID__c`
  - `Intake_Status__c`
  - `Intake_Source__c`
- Field-level write access:
  - `Name`
  - `StageName`
  - `CloseDate`
  - `Description`
  - `External_Source_ID__c`
  - `Intake_Status__c`
  - `Intake_Source__c`
- Ability to describe Opportunity metadata through the Salesforce API.

Recommended:

- Use a dedicated Salesforce integration user.
- Use a dedicated Permission Set rather than broad profile changes.
- Grant only the object and field access required above.
- Keep the integration user active and monitored.

Salesforce recommends permission sets for reusable user access management. See
Salesforce Help: [User Permissions](https://help.salesforce.com/s/articleView?id=platform.admin_userperms.htm&language=en_US).

## 6. BidLens deployment configuration

This section is for the BidLens operator/deployer. Customer Salesforce Admins
usually provide the Salesforce values, but should not manage BidLens secrets
directly unless they also operate the BidLens deployment.

### Salesforce environment variables

BidLens reads these variables:

| Variable | Source | Description |
| --- | --- | --- |
| `SALESFORCE_INSTANCE_URL` | Salesforce / customer | Salesforce My Domain URL used for OAuth and API calls, for example `https://customer.my.salesforce.com`. |
| `SALESFORCE_CLIENT_ID` | Salesforce Connected App | Consumer Key from the Connected App. |
| `SALESFORCE_CLIENT_SECRET` | Salesforce Connected App | Consumer Secret from the Connected App. Treat as a secret. |
| `SALESFORCE_REDIRECT_URI` | BidLens operator | Exact OAuth callback URL registered in Salesforce. |

Production/staging callback example:

```text
https://<bidlens-domain>/api/salesforce/oauth/callback
```

Local callback example:

```text
http://127.0.0.1:8000/api/salesforce/oauth/callback
```

### Credential storage

After OAuth succeeds, BidLens stores:

- safe Salesforce organization/user metadata;
- encrypted access token;
- encrypted refresh token;
- connection status and timestamps.

Storage behavior:

- Tokens are stored encrypted in PostgreSQL for hosted deployments.
- Tokens are scoped to the BidLens workspace.
- Tokens are not displayed in BidLens UI.
- Tokens are not stored only in process memory.

Operational warning:

- BidLens derives integration credential encryption from `SECRET_KEY`.
- Rotating `SECRET_KEY` without a credential migration or reauthorization plan
  can make existing encrypted Salesforce tokens undecryptable.

Keep `SALESFORCE_CLIENT_SECRET`, `SECRET_KEY`, access tokens, and refresh tokens
out of support tickets, screenshots, and logs.

## 7. Connecting Salesforce in BidLens

These steps are performed by a BidLens Workspace Admin.

### During initial workspace setup

1. Sign in to BidLens.
2. Open the workspace setup flow.
3. Open Connect Business Systems.
4. In the CRM section, find Salesforce.
5. Click Connect.
6. Complete Salesforce authorization.
7. After Salesforce redirects back, BidLens returns to Organization Setup.

### After the workspace is live

1. Sign in to BidLens as a Workspace Admin.
2. Open Workspace Management.
3. Open Integrations.
4. Open Salesforce.
5. Click Connect Salesforce.
6. Complete Salesforce authorization.
7. After Salesforce redirects back, BidLens returns to the Salesforce configuration page.

### Reading the Salesforce configuration page

The Salesforce configuration page is:

```text
/workspace-management/business-systems/salesforce
```

It shows:

- Connection Status.
- Connected Organization.
- Salesforce Organization ID, if provided by Salesforce.
- Connected User.
- Instance URL.
- Connected Date.
- Last Successful Connection.
- Last Successful Sync.
- Salesforce Readiness.
- Basic Sync Behavior.
- Future Configuration placeholders.

### Test Connection

After connecting:

1. Open Workspace Management → Integrations → Salesforce.
2. Click Test Connection.
3. Confirm BidLens shows Salesforce connection verified.

If Test Connection fails, use the troubleshooting section below.

### Validate Setup

Use Validate Setup after connecting Salesforce and after any Salesforce schema,
permission, Connected App, or picklist change.

Validate Setup is deeper than Test Connection. It checks OAuth access,
Opportunity metadata, required fields, required picklist values, and additional
required Opportunity fields. It does not create or modify Salesforce records.

1. Open Workspace Management → Integrations → Salesforce.
2. Click Validate Setup in the Salesforce Readiness section.
3. Review the overall result:
   - Ready
   - Ready with warnings
   - Action required
4. Review each Passed, Warning, or Failed check.
5. Correct any failed Salesforce configuration items before relying on sync.

### Reconnect or Reauthorize

Use Reconnect / Reauthorize when:

- the Salesforce user changed;
- Salesforce access was revoked;
- BidLens shows Requires Reauthorization;
- the Connected App or permissions were changed.

### Disconnect

Use Disconnect to remove BidLens' locally stored Salesforce tokens for this
workspace.

Disconnect behavior:

- Clears encrypted Salesforce access and refresh tokens from BidLens.
- Sets the workspace Salesforce connection to Not Connected.
- Preserves existing local Salesforce Opportunity IDs, URLs, and sync history.
- Does not currently guarantee remote token revocation inside Salesforce.

## 8. First-sync acceptance test

Use this test after setup to confirm the integration works end to end.

### Step A: Confirm OAuth connection

1. In BidLens, open Workspace Management → Integrations → Salesforce.
2. Confirm Connection Status is Connected.
3. Confirm Connected Organization shows the expected Salesforce instance URL.
4. Click Test Connection.
5. Confirm the page reports Salesforce connection verified.
6. Click Validate Setup.
7. Confirm the Salesforce Readiness result is Ready before testing Opportunity sync.

### Step B: Confirm Opportunity eligibility

Choose a BidLens opportunity that is:

- in the same workspace;
- qualified;
- not archived;
- has a source record ID.

If the opportunity is not qualified or is archived, current Salesforce promotion
logic rejects it.

### Step C: Trigger Salesforce create/link

There are two supported paths:

1. A user marks an eligible opportunity Interested.
2. A Workspace Admin uses the available Salesforce/CRM promotion action.

Current Interested behavior:

- A user marking an eligible opportunity Interested can trigger Salesforce create/link behavior.
- If Salesforce sync fails, the opportunity should remain in the user's shortlist.
- The user may see a Salesforce warning, but the Interested decision is not removed solely because Salesforce failed.

### Step D: Verify Salesforce result

BidLens should either:

- link to an existing Salesforce Opportunity where `External_Source_ID__c`
  equals the BidLens `source_record_id`; or
- create a new Salesforce Opportunity.

Verify in BidLens:

- Salesforce Opportunity ID is stored.
- Salesforce Opportunity URL is stored.
- Opportunity history includes Salesforce sync activity.

Verify in Salesforce:

- Opportunity exists.
- `External_Source_ID__c` equals the BidLens source record ID.
- `Intake_Status__c` is `Prospect_Feed`.
- Stage is `Prospecting` for newly created records.
- Close Date matches the BidLens opportunity deadline, or the fallback date if no deadline existed.

### Step E: Verify supported source update sync

After a linked BidLens opportunity receives a later source update, BidLens can
push supported changed fields to Salesforce:

- title → `Name`
- response deadline → `CloseDate`
- description / description text → `Description`

Verify:

1. A source update changes one of those supported fields.
2. BidLens records a source update event.
3. The event shows Salesforce sync status `succeeded`.
4. The Salesforce Opportunity reflects the updated field.

## 9. Connection lifecycle states

### Not Connected

Meaning:

- No usable Salesforce tokens are stored for this BidLens workspace.

Admin action:

- Click Connect Salesforce.

Can normal Salesforce sync proceed?

- No.

### Connected

Meaning:

- BidLens has stored Salesforce authorization for the workspace and can attempt outbound Salesforce actions.

Admin action:

- Optionally click Test Connection to verify current access.

Can normal Salesforce sync proceed?

- Yes, unless a later Salesforce API call fails due to permissions, schema, rate limits, or record access.

### Connection Error

Meaning:

- BidLens could not validate or refresh the Salesforce connection.

Admin action:

1. Click Test Again.
2. If the error persists, click Reauthorize.
3. Confirm Salesforce permissions, API access, and Connected App policies.

Can normal Salesforce sync proceed?

- Not reliably.

### Requires Reauthorization

Meaning:

- Salesforce access expired, was revoked, or the refresh token is no longer accepted.

Admin action:

- Click Reauthorize.

Can normal Salesforce sync proceed?

- No, not until reauthorization succeeds.

## 10. Troubleshooting

### Redirect URI mismatch

Symptom:

- Salesforce authorization fails before returning to BidLens, or Salesforce reports a redirect/callback mismatch.

Likely cause:

- Connected App callback URL does not exactly match `SALESFORCE_REDIRECT_URI`.

Corrective steps:

1. Confirm the BidLens deployment URL.
2. Confirm `SALESFORCE_REDIRECT_URI`.
3. Update the Salesforce Connected App Callback URL to match exactly.
4. Try Connect Salesforce again.

### Invalid client ID or secret

Symptom:

- OAuth starts but callback/token exchange fails.

Likely cause:

- `SALESFORCE_CLIENT_ID` or `SALESFORCE_CLIENT_SECRET` does not match the Connected App.

Corrective steps:

1. Copy the Consumer Key into `SALESFORCE_CLIENT_ID`.
2. Copy the Consumer Secret into `SALESFORCE_CLIENT_SECRET`.
3. Restart/redeploy BidLens if required by the hosting platform.
4. Try Connect Salesforce again.

Do not send the Consumer Secret in support tickets.

### Missing OAuth scope

Symptom:

- OAuth succeeds but API calls or refresh later fail.

Likely cause:

- Connected App does not include `api` or `refresh_token` / `offline_access`.

Corrective steps:

1. Add required OAuth scopes to the Connected App.
2. Reauthorize Salesforce in BidLens.

### User lacks Connected App access

Symptom:

- Salesforce reports the user is not approved for access.

Likely cause:

- Connected App Permitted Users is Admin approved users are pre-authorized, but the user lacks profile or permission-set access.

Corrective steps:

1. Grant the user access to the Connected App through a Permission Set or Profile.
2. Try Connect Salesforce again.

### API access disabled

Symptom:

- Salesforce API calls fail with API access errors.

Likely cause:

- Salesforce org edition lacks API access, or the authorizing user lacks API Enabled.

Corrective steps:

1. Confirm the Salesforce edition supports API access.
2. Confirm the user has API Enabled.
3. If on Professional Edition, confirm API access has been purchased/enabled.

### Missing custom field

Symptom:

- Opportunity creation fails or Validate Setup reports unavailable mapped fields.

Likely cause:

- One of the expected custom fields does not exist.

Corrective steps:

1. Create the missing custom field on Opportunity.
2. Use the exact API name documented in this guide.
3. Grant field-level access.
4. Test again.

### Incorrect field API name

Symptom:

- Field appears to exist in Salesforce, but BidLens still reports it missing or Salesforce rejects payloads.

Likely cause:

- The field label is similar, but the API name differs.

Corrective steps:

1. In Object Manager → Opportunity → Fields & Relationships, check Field Name/API Name.
2. Confirm exact names:
   - `External_Source_ID__c`
   - `Intake_Status__c`
   - `Intake_Source__c`
3. Correct the Salesforce field or update BidLens code before retrying.

### Missing Prospecting StageName value

Symptom:

- Opportunity creation fails with invalid StageName.

Likely cause:

- `Prospecting` is not an active StageName value for the Opportunity record type/context.

Corrective steps:

1. Add or enable `Prospecting` for Opportunity Stage.
2. Confirm it is available for the relevant record type, if record types are used.
3. Retry the sync.

### Missing Prospect_Feed Intake Status value

Symptom:

- Opportunity create or intake-status update fails.

Likely cause:

- `Intake_Status__c` does not accept `Prospect_Feed`.

Corrective steps:

1. Add `Prospect_Feed` as an active value for `Intake_Status__c`.
2. Grant field-level write access.
3. Retry the sync.

### Invalid Intake Source value

Symptom:

- Opportunity creation fails when writing `Intake_Source__c`.

Likely cause:

- No active values exist, or the selected value is unavailable for the record type.

Corrective steps:

1. Add the missing opportunity source values as active `Intake_Source__c` picklist values: `SAM`, `Grants.gov`, and `GovWin`.
2. Confirm those values are available for the relevant record type.
3. Retry the sync.

### Additional required Salesforce Opportunity fields

Symptom:

- BidLens reports that Salesforce Opportunity has required createable fields outside the current payload.

Likely cause:

- The Salesforce org requires additional Opportunity fields that BidLens V1 does not set.

Corrective steps:

1. Review required Opportunity fields in Salesforce.
2. Make the field optional, provide a Salesforce default, or add automation that fills it.
3. If the field must be supplied by BidLens, this requires a BidLens field mapping enhancement.

### Revoked or expired refresh authorization

Symptom:

- BidLens shows Requires Reauthorization.

Likely cause:

- Salesforce refresh token expired, was revoked, or the user/app access changed.

Corrective steps:

1. Click Reauthorize in BidLens.
2. Complete Salesforce OAuth again.
3. Run Test Connection.

### Deleted or inaccessible linked Opportunity

Symptom:

- A previously linked BidLens opportunity fails to update Salesforce.

Likely cause:

- Salesforce Opportunity was deleted, merged, archived by Salesforce policy, or the integration user no longer has access.

Corrective steps:

1. Check the Salesforce Opportunity ID stored in BidLens.
2. Confirm the record exists and is accessible to the integration user.
3. Restore permissions or relink through a future data-correction workflow.

### Duplicate or colliding source record IDs

Symptom:

- BidLens links to an unexpected existing Salesforce Opportunity.

Likely cause:

- Multiple Salesforce records share the same `External_Source_ID__c`, or different source systems use colliding source record IDs.

Corrective steps:

1. Search Salesforce for duplicate `External_Source_ID__c` values.
2. Make the field unique if appropriate.
3. Review the affected BidLens opportunity source record IDs.

### Salesforce rate limits or temporary errors

Symptom:

- Sync fails intermittently after previously working.

Likely cause:

- Salesforce rate limiting, network timeout, temporary service error, or maintenance.

Corrective steps:

1. Wait and retry the relevant action if available.
2. Check Salesforce API limits and status.
3. Review BidLens source update logs for failed Salesforce status.

### Connection succeeds but sync later fails

Symptom:

- Test Connection succeeds, but create/update fails later.

Likely cause:

- OAuth is valid, but Opportunity object permissions, field permissions, picklist values, required fields, record type settings, or record access block the specific operation.

Corrective steps:

1. Run Validate Setup from Workspace Management → Integrations → Salesforce.
2. Confirm all required fields and picklist values.
3. Confirm the authorizing user has object and field write permissions.
4. Retry the sync.

## 11. Current V1 limitations

BidLens Salesforce V1 intentionally has a narrow scope.

Current limitations:

- Field mapping is hard-coded.
- New Salesforce Opportunities use `StageName = Prospecting`.
- BidLens writes `Intake_Status__c = Prospect_Feed`.
- BidLens writes `Intake_Source__c` as the originating opportunity source, such as `SAM`, `Grants.gov`, or `GovWin`; BidLens itself is not an intake source value.
- Duplicate lookup uses `External_Source_ID__c = BidLens source_record_id`.
- One Salesforce organization can be connected per BidLens workspace.
- No general bidirectional sync.
- No configurable default owner.
- No configurable default record type.
- No configurable sync direction.
- No configurable automatic push rules.
- No automatic retry queue or reconciliation for failed syncs.
- Later source-update sync is limited to `Name`, `CloseDate`, and `Description`.
- Disconnect clears BidLens-stored encrypted credentials but may not revoke the Salesforce token remotely.
- BidLens does not currently provide a Salesforce managed package or metadata installer.

## 12. Final checklists

### Salesforce Admin checklist

- [ ] Confirm Salesforce edition/user license supports API access.
- [ ] Create or identify the BidLens Connected App.
- [ ] Enable OAuth Settings.
- [ ] Set Callback URL to `https://<bidlens-domain>/api/salesforce/oauth/callback`.
- [ ] Add OAuth scopes `api` and `refresh_token` / `offline_access`.
- [ ] Configure refresh-token policy.
- [ ] Configure Permitted Users policy.
- [ ] Grant Connected App access to the authorizing user.
- [ ] Confirm Opportunity object read/create/update access.
- [ ] Create/verify custom fields:
  - [ ] `External_Source_ID__c`
  - [ ] `Intake_Status__c`
  - [ ] `Intake_Source__c`
- [ ] Confirm `StageName` includes `Prospecting`.
- [ ] Confirm `Intake_Status__c` accepts `Prospect_Feed`.
- [ ] Confirm `Intake_Source__c` includes active values for `SAM`, `Grants.gov`, and `GovWin`.
- [ ] Grant field-level access for all required fields.
- [ ] Provide Consumer Key and Consumer Secret securely to the BidLens operator.

### BidLens operator/deployment checklist

- [ ] Set `SALESFORCE_INSTANCE_URL`.
- [ ] Set `SALESFORCE_CLIENT_ID`.
- [ ] Set `SALESFORCE_CLIENT_SECRET`.
- [ ] Set `SALESFORCE_REDIRECT_URI`.
- [ ] Confirm `SALESFORCE_REDIRECT_URI` exactly matches the Connected App callback URL.
- [ ] Confirm hosted deployment uses PostgreSQL.
- [ ] Confirm `SECRET_KEY` is strong and stable.
- [ ] Document the operational plan before rotating `SECRET_KEY`.
- [ ] Apply database migrations before deployment.
- [ ] Do not expose Salesforce secrets in logs or tickets.

### BidLens Workspace Admin checklist

- [ ] Sign in as a Workspace Admin.
- [ ] During setup, open Connect Business Systems; after go-live, open Workspace Management → Integrations → Salesforce.
- [ ] Click Connect Salesforce.
- [ ] Complete Salesforce authorization.
- [ ] Confirm Connection Status is Connected.
- [ ] Review Connected Organization metadata.
- [ ] Click Test Connection.
- [ ] Click Validate Setup and confirm Salesforce Readiness is Ready.
- [ ] Confirm a qualified, non-archived opportunity with a source record ID can create/link in Salesforce.
- [ ] Confirm Salesforce Opportunity ID and URL appear in BidLens.
- [ ] Confirm a supported later source update can sync to Salesforce.

## Implementation reference

This section is for BidLens maintainers. Customer setup instructions above
should remain free of internal implementation details where possible.

Relevant source files:

- `src/bidlens/config.py` — Salesforce environment variables.
- `src/bidlens/models.py` — `SalesforceConnection`, `SalesforceOAuthState`, Opportunity Salesforce fields, source update audit fields.
- `src/bidlens/services/integration_credentials.py` — encrypted credential storage derived from `SECRET_KEY`.
- `src/bidlens/services/salesforce.py` — OAuth, token refresh, test connection, describe, query, create, update.
- `src/bidlens/services/salesforce_promotion.py` — create/link/push behavior and payload construction.
- `src/bidlens/services/opportunity_monitor.py` — outbound sync for supported source updates.
- `src/bidlens/routes/api.py` — OAuth routes, Interested behavior, admin push/create APIs.
- `src/bidlens/routes/integrations.py` — Salesforce configuration page routes.
- `src/bidlens/routes/connect_sources.py` — pre-live outbound integrations entry point.
- `src/bidlens/templates/salesforce_configuration.html` — Salesforce configuration UI.
- `src/bidlens/templates/outbound_integrations.html` — setup Business Systems UI.
- `src/bidlens/templates/integrations.html` — Workspace Management Integrations UI.
- `alembic/versions/e6f7a8b9c0d1_add_salesforce_connections.py` — workspace-scoped connection tables.
- `alembic/versions/3d4e5f6a7b8c_add_salesforce_opportunity_reference.py` — Salesforce Opportunity reference columns.
- `alembic/versions/c3d4e5f6a7b8_add_crm_push_status.py` — local CRM promotion marker columns.
- `alembic/versions/9f0a1b2c3d4e_add_source_update_audit_fields.py` — source update sync audit fields.

Focused tests:

- `tests/test_salesforce_configuration.py`
- `tests/test_salesforce_onboarding_redirect.py`
- `tests/test_interested_salesforce.py`
- `tests/test_opportunity_monitor.py`
