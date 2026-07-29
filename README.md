# BidLens

A lightweight web app that helps small businesses triage SAM.gov Contract Opportunities. Decision-first design with a calm UI, short default lists, and stateful saved items displayed on a simple calendar view.

## Features

- **Feed Page**: Browse solicitations and RFIs in separate tabs, sorted by deadline
- **Opportunity Details**: View full details, save opportunities, set internal deadlines, add notes
- **My Bids**: Track saved opportunities with status (saved/in-progress/dropped)
- **Calendar View**: See your saved solicitations organized by deadline

## Tech Stack

- **Backend**: Python FastAPI
- **Frontend**: Server-rendered Jinja2 templates
- **Database**: PostgreSQL (via SQLAlchemy)
- **Auth**: Dev mode email login (magic link structure ready for later)

## Running the App

The app runs automatically on port 5000. To seed the database with test opportunities:

```bash
python seed.py
```

## Development Reset

To reset the local development database for onboarding/QA testing:

```bash
python scripts/reset_dev.py
```

or:

```bash
make reset-dev
```

This utility is local-development only. It preserves `joshuatlaven@gmail.com` as the Platform Owner login, creates/reuses a local-only internal `BidLens Platform` organization for the current legacy `users.organization_id` constraint, and removes customer organizations, customer workspaces, invitations, memberships, company profiles, connector configuration, opportunities, history, and other customer-owned records.

## Environment Variables

- `SAM_API_KEY`: SAM.gov API key used for opportunity pulls and notice description fetches
- `DATABASE_URL`: database connection string
- `SECRET_KEY`: Session encryption key (defaults to dev key)
- `SALESFORCE_INSTANCE_URL`: Salesforce My Domain URL, for example `https://your-domain.my.salesforce.com`
- `SALESFORCE_CLIENT_ID`: Salesforce Connected App consumer key
- `SALESFORCE_CLIENT_SECRET`: Salesforce Connected App consumer secret
- `SALESFORCE_REDIRECT_URI`: OAuth callback URL, for example `http://127.0.0.1:8000/api/salesforce/oauth/callback`
- `MICROSOFT_CLIENT_ID`: Microsoft Entra application client ID for delegated user connection
- `MICROSOFT_CLIENT_SECRET`: Microsoft Entra application client secret
- `MICROSOFT_REDIRECT_URI`: Microsoft OAuth callback URL, for example `http://127.0.0.1:8000/integrations/microsoft/oauth/callback`
- `MICROSOFT_TENANT_ID`: Microsoft authority tenant mode, such as `common`, `organizations`, or a specific tenant ID; defaults to `common`
- `ENABLE_INTERNAL_SCHEDULER`: set to `true` only when this process should start APScheduler
- `AUTO_CREATE_SCHEMA`: set to `false` in hosted environments that use Alembic migrations
- `SESSION_COOKIE_SECURE`: set to `true` when serving over HTTPS
- `BIDLENS_VALIDATE_DEPLOYMENT`: optional explicit hosted-config validation flag; validation also runs automatically when `AUTO_CREATE_SCHEMA=false`
- `RESEND_API_KEY`: Resend API key used by the Daily Brief Email cron service
- `DAILY_BRIEF_EMAIL_FROM`: verified sender address for Daily Brief emails
- `BIDLENS_APP_BASE_URL`: public BidLens base URL used in Daily Brief email links
- `PORT`: platform-provided web port for hosted startup commands

## Startup Commands

Local development with reload:

```bash
make dev
```

Private staging web process:

```bash
PYTHONPATH=src uvicorn bidlens.main:app --host 0.0.0.0 --port "$PORT"
```

Local SQLite database:

```bash
DATABASE_URL=sqlite:///./bidlens.db
AUTO_CREATE_SCHEMA=true
```

Hosted PostgreSQL database:

```bash
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DBNAME
AUTO_CREATE_SCHEMA=false
```

Apply schema changes with Alembic:

```bash
alembic upgrade head
```

For hosted staging, run with `ENABLE_INTERNAL_SCHEDULER=false`, `AUTO_CREATE_SCHEMA=false`, and `SESSION_COOKIE_SECURE=true`.

## Switching Databases

Keep `.env` as the safe local default:

```bash
DATABASE_URL=sqlite:///bidlens.db
```

To run against local SQLite:

```bash
source scripts/use-local.sh
python -m uvicorn src.bidlens.main:app --host 127.0.0.1 --port 8000
```

To run against Railway PostgreSQL, create a developer-local credentials file once:

```bash
cp .env.railway.example .env.railway.local
# edit .env.railway.local with your Railway credentials
```

Then switch the current shell to Railway:

```bash
source scripts/use-railway.sh
python -m uvicorn src.bidlens.main:app --host 127.0.0.1 --port 8000
```

`.env.railway.local` is ignored by git and should never be committed.

## Disposable PostgreSQL Validation

Use a temporary hosted PostgreSQL database before the first private staging deploy.

```bash
export DATABASE_URL='postgresql://USER:PASSWORD@HOST:5432/DBNAME'
export AUTO_CREATE_SCHEMA=false
export ENABLE_INTERNAL_SCHEDULER=false

alembic upgrade head
alembic current
```

Successful migration output should end at the current Alembic head, for example:

```text
d5e6f7a8b9c0 (head)
```

Then start the web process against the migrated database:

```bash
PYTHONPATH=src uvicorn bidlens.main:app --host 127.0.0.1 --port 8012
```

Smoke-test checklist:

- Open `/health` and confirm `{"status":"ok"}`.
- Log in through the staging login page.
- Load one database-backed page, such as Home or Feed.
- Create one safe test record, such as a test workspace or invitation.
- Restart the app.
- Verify the test record persists after restart.

Failures that block staging:

- `alembic upgrade head` fails on the empty PostgreSQL database.
- `alembic current` does not report the head revision.
- App startup logs a database connection or missing-table error with `AUTO_CREATE_SCHEMA=false`.
- `/health` does not return HTTP 200.
- A record created before restart is missing after restart.

## Job Run Logging

BidLens records durable `JobRun` rows for important automated or externally triggered workspace operations. A job type is the stable category of work, such as `sam_ingest`, `grants_ingest`, or `daily_snapshot`. A job run is one execution of that job for one workspace-scoped organization.

`JobRun` is intentionally separate from `IngestionRun`:

- `JobRun` answers whether the outer scheduled/manual operation ran, when it ran, and its overall outcome.
- `IngestionRun` answers what happened inside a specific opportunity-source ingestion.

Use `bidlens.services.job_runs.start_job_run`, `complete_job_run`, and `fail_job_run` from future standalone commands, Railway cron entry points, or manual operational scripts. Store job-specific counts in `details_json` rather than adding new columns for every connector metric.

## Standalone Operational Jobs

The hosted web process should serve web requests only. Operational work can be run independently with:

```bash
PYTHONPATH=src python -m bidlens.jobs.run_sam_refresh
PYTHONPATH=src python -m bidlens.jobs.run_sam_ingest
PYTHONPATH=src python -m bidlens.jobs.run_grants_ingest
PYTHONPATH=src python -m bidlens.jobs.run_daily_snapshots
PYTHONPATH=src python -m bidlens.jobs.run_daily_brief_emails
PYTHONPATH=src python -m bidlens.jobs.run_outlook_conversation_sync
```

Each command defaults to `--trigger-type scheduled`. For local manual testing, pass:

```bash
--trigger-type manual
```

The Daily Snapshot and Daily Brief Email commands also accept:

```bash
--snapshot-date YYYY-MM-DD
```

Staging assumptions:

```bash
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DBNAME
AUTO_CREATE_SCHEMA=false
ENABLE_INTERNAL_SCHEDULER=false
```

For Railway Cron, use the production-safe SAM refresh command:

```bash
PYTHONPATH=src python -m bidlens.jobs.run_sam_refresh
```

Schedule it with cron expression `0 12 * * *` for approximately 5:00 AM
Phoenix time. Keep `ENABLE_INTERNAL_SCHEDULER=false` on the Railway web service.

Run tracked Outlook synchronization from one separate Railway Cron service:

```text
Schedule: */15 * * * *
Command: PYTHONPATH=src python -m bidlens.jobs.run_outlook_conversation_sync
```

The command checks only conversations initiated and tracked by BidLens. Keep
`ENABLE_INTERNAL_SCHEDULER=false` on the cron service as well.

Each standalone job creates one `JobRun` per eligible organization. SAM.gov and Grants.gov jobs also preserve their existing `IngestionRun` records for source-specific ingestion history. Daily Snapshot creates one organization-level `JobRun` with aggregate user counts.
Daily Brief Email creates one organization-level `JobRun` and one durable
delivery record per attempted user/snapshot date. Successful deliveries are not
sent twice on rerun; failed deliveries can be retried.

Exit-code policy:

- `0`: all processed organizations ended in `success`, `paused`, or intentional `skipped`.
- Nonzero: one or more organizations ended in `failed` or `partial_success`.

These commands are intended to become Railway cron commands in a later phase. Candidate cron commands:

```bash
PYTHONPATH=src python -m bidlens.jobs.run_sam_refresh
PYTHONPATH=src python -m bidlens.jobs.run_sam_ingest
PYTHONPATH=src python -m bidlens.jobs.run_grants_ingest
PYTHONPATH=src python -m bidlens.jobs.run_daily_snapshots
PYTHONPATH=src python -m bidlens.jobs.run_daily_brief_emails
```

Do not run overlapping copies of the same job yet; distributed locking is deferred.

## Unified Intake Source-Material Storage

Local development may use `SOURCE_MATERIAL_STORAGE_BACKEND=local` with
`SOURCE_MATERIAL_LOCAL_ROOT=.bidlens/source-materials`. Hosted deployments reject
local storage because Railway service disk is ephemeral.

Production and staging must use a private S3-compatible bucket:

```text
SOURCE_MATERIAL_STORAGE_BACKEND=s3
SOURCE_MATERIAL_S3_BUCKET=bidlens-source-materials
SOURCE_MATERIAL_S3_ENDPOINT_URL=https://your-s3-compatible-endpoint
SOURCE_MATERIAL_S3_REGION=us-east-1
SOURCE_MATERIAL_S3_ACCESS_KEY_ID=replace-with-access-key
SOURCE_MATERIAL_S3_SECRET_ACCESS_KEY=replace-with-secret-key
SOURCE_MATERIAL_S3_PATH_PREFIX=bidlens/source-materials
SOURCE_MATERIAL_S3_USE_SSL=true
```

`SOURCE_MATERIAL_S3_ENDPOINT_URL` may be omitted when the standard AWS S3 endpoint
is appropriate. The bucket must remain private. BidLens never renders permanent
object URLs: authenticated downloads are authorized against the workspace and
draft or published Opportunity, then streamed through the application.

Storage keys are generated independently of filenames and retain their
organization, workspace, and intake-draft scope after publication. Uploaded bytes
are stored only in object storage; the database stores hashes and metadata.

Before enabling uploads in a hosted environment:

1. Provision the private bucket and least-privilege credentials for object put,
   get, head, list, and delete operations under the configured path prefix.
2. Configure the variables above and keep `SOURCE_MATERIAL_S3_USE_SSL=true`.
3. Run migrations before starting the web process.
4. Verify PDF, DOCX, and EML upload, authenticated retrieval, application restart,
   and abandoned-draft cleanup with non-sensitive fixtures.

The report-only `reconcile_source_materials` service identifies missing objects,
unreferenced objects, and expired unpublished materials without deleting orphaned
bucket objects automatically. Abandoned-draft cleanup deletes only unpublished,
unassociated materials and retains metadata when storage deletion fails so the
operation can be retried safely.

Run the bounded report with:

```bash
PYTHONPATH=src python scripts/reconcile_source_materials.py --organization-id 1 --workspace-id 1
```

## Platform Operations

Platform Owners can inspect durable operational job history at:

```text
/platform/operations
```

The page is read-only and Platform-only. It uses `JobRun` as the primary source for cross-workspace diagnostics, with filters for organization, job type, status, and date range. Run details show readable aggregate metrics from `details_json` and safe error information. Workspace Admins and Members should not have access.

## Salesforce Integration

For customer-facing setup instructions, see
[BidLens Salesforce V1 Setup Guide](docs/integrations/salesforce_setup_guide.md).

BidLens uses the Salesforce OAuth 2.0 Authorization Code flow with PKCE. Each
workspace has its own Salesforce connection record, so one customer workspace
cannot read or reuse another workspace's Salesforce authorization.

The OAuth callback stores safe connection metadata and encrypted Salesforce
tokens in the application database. Access and refresh tokens are never rendered
in the UI. Credential encryption is derived from `SECRET_KEY`, so rotating
`SECRET_KEY` requires a credential-rotation plan or existing encrypted
Salesforce tokens will no longer decrypt.

Connected App settings:

- Enable OAuth settings.
- Callback URL: set this to the exact `SALESFORCE_REDIRECT_URI` value used by BidLens.
- OAuth scopes: include `api` and `refresh_token` / `offline_access`.
- Client type: confidential app with a consumer secret.
- The authorizing Salesforce user needs access to describe, query, create, and update `Opportunity` records.
- The authorizing Salesforce user needs field access for `Opportunity.External_Source_ID__c`, `Opportunity.Intake_Status__c`, and `Opportunity.Intake_Source__c`.

Expected Salesforce Opportunity configuration:

- `StageName` includes `Prospecting`.
- `Intake_Status__c` supports `Prospect_Feed`.
- `Intake_Source__c` includes active values for the opportunity sources BidLens can send: `SAM`, `Grants.gov`, and `GovWin`.
- `External_Source_ID__c` should be configured as an External ID and should be unique if the customer wants Salesforce to enforce duplicate protection.

Workspace authorization:

1. Start BidLens with the Salesforce environment variables configured.
2. Sign in to BidLens as a workspace admin.
3. Open Workspace Management → Integrations → Salesforce, or during pre-live setup open Connect Business Systems.
4. Click Connect Salesforce.
5. Sign in to Salesforce and approve the Connected App.
6. After the callback succeeds, BidLens stores the workspace-scoped encrypted connection and returns to the setup or Salesforce configuration page.

Connection lifecycle:

- Connected workspaces can test the connection from `/workspace-management/business-systems/salesforce`.
- Workspace Admins can use Validate Setup on the Salesforce configuration page to verify OAuth access, Opportunity fields, and required picklist values without creating Salesforce records.
- Reconnect / Reauthorize starts a new OAuth flow for the same workspace.
- Disconnect clears the locally stored encrypted access and refresh tokens while preserving existing local Salesforce opportunity references and sync history.
- If Salesforce revokes or expires the refresh token, BidLens marks the connection as requiring reauthorization.

Current Salesforce capabilities:

- Interested and qualified opportunities may be created in or linked to Salesforce.
- Existing Salesforce Opportunities are matched by `External_Source_ID__c`.
- Linked opportunities store the Salesforce Opportunity ID and URL in BidLens.
- Source updates for linked opportunities may push changes to Salesforce `Name`, `CloseDate`, and `Description`.
- BidLens records Salesforce sync outcomes in opportunity history and source-update audit events.
- BidLens does not currently perform general bidirectional synchronization.
- Field mapping, default owner, default record type, sync direction, and automatic push rules are placeholders for a future release.

## Microsoft 365 Connection

BidLens supports per-user Microsoft account connection management as a foundation for future Microsoft-backed workflows. Each connection belongs to exactly one BidLens user in one workspace. Workspace Admins can see non-sensitive adoption status, but they cannot test, refresh, disconnect, decrypt, or manage another user’s Microsoft credentials.

Current Microsoft capability is intentionally limited to delegated identity connection, intentional user-initiated email sending, and persistence of provider metadata for conversations initiated from BidLens:

- Authorization Code flow with PKCE and durable short-lived OAuth state.
- Encrypted access and refresh token storage using the same server-side credential encryption helper as Salesforce/GovWin.
- Microsoft identity verification against the connected account.
- User-initiated Test Connection, Reconnect, and Disconnect.
- User-initiated Opportunity Conversation email creates and sends one Graph draft using an immutable message ID.
- After sending, BidLens retrieves only that exact message by immutable ID to persist its message and conversation identifiers.
- No mailbox scanning, folder browsing, webhooks, attachment access, inbound reply synchronization, or mailbox-wide Opportunity Conversation discovery.

Minimal delegated Microsoft scopes requested:

- `openid`, `profile`, and `email`: identify the connected Microsoft user.
- `offline_access`: maintain delegated access through refresh tokens.
- `User.Read`: call Microsoft Graph `/me` for identity verification only.
- `Mail.Send`: send an opportunity-related email only when the connected user intentionally submits the Start Conversation form.
- `Mail.ReadWrite`: create the single outbound draft and retrieve that exact BidLens-initiated message by immutable ID for tracking metadata.

Configure the Microsoft Entra app redirect URI to the exact `MICROSOFT_REDIRECT_URI` value used by BidLens. For local development this is commonly:

```text
http://127.0.0.1:8000/integrations/microsoft/oauth/callback
```

## Rotating SAM API Key

1. Generate a new SAM.gov API key in your SAM account.
2. Update `SAM_API_KEY` in the project-root [`.env`](/Users/joshlaven/Desktop/BidLens/bidlens/.env) file.
3. Run the environment check script:

```bash
python scripts/check_env.py
```

4. Restart the BidLens app so the running process picks up the new key.
5. Optionally verify the new key with `curl` against the SAM.gov API before or after restart.

BidLens reads `SAM_API_KEY` from the project-root `.env` via [src/bidlens/config.py](/Users/joshlaven/Desktop/BidLens/bidlens/src/bidlens/config.py), and the check script only prints a masked version of the key.

## Project Structure

```
src/bidlens/
├── main.py          # FastAPI app entry point
├── config.py        # Configuration settings
├── database.py      # SQLAlchemy setup
├── models.py        # Data models
├── auth.py          # Session authentication
├── routes/          # API routes
│   ├── auth.py      # Login/logout
│   └── opportunities.py  # Core functionality
├── templates/       # Jinja2 HTML templates
└── static/css/      # Stylesheets
```

## Data Model

- **Opportunity**: Global records from SAM.gov
- **User**: Email-based accounts with is_paid flag
- **UserOpportunity**: Per-user state (saved/status/deadline/notes)
# On-demand communication summaries

Opportunity Communication summaries are generated only when an authorized user submits **Generate Summary** or **Refresh Summary**. Each action makes one model request; page loads, Outlook synchronization, and scheduled jobs make none. Configure the feature with `AI_SUMMARY_PROVIDER`, `AI_SUMMARY_API_KEY`, `AI_SUMMARY_MODEL`, `AI_SUMMARY_MAX_INPUT_CHARS`, `AI_SUMMARY_MAX_OUTPUT_TOKENS`, `AI_SUMMARY_TEMPERATURE`, `AI_SUMMARY_TIMEOUT_SECONDS`, and `AI_SUMMARY_MAX_RETRIES` (default `0`, preserving one HTTP attempt per click). `AI_SUMMARY_BASE_URL` is optional. The API key and model fall back to `OPENAI_API_KEY` and `OPENAI_MODEL` for compatibility.

Cost is therefore approximately the provider's price for one bounded input plus one bounded output per click. Input and output caps are the first cost controls; future controls could add per-workspace quotas or cooldowns at the POST/service boundary. The application stores the structured summary and usage metadata is logged when supplied, but prompts, raw responses, credentials, and chain-of-thought are not persisted.
