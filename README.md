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
- `ENABLE_INTERNAL_SCHEDULER`: set to `true` only when this process should start APScheduler
- `AUTO_CREATE_SCHEMA`: set to `false` in hosted environments that use Alembic migrations
- `SESSION_COOKIE_SECURE`: set to `true` when serving over HTTPS
- `BIDLENS_VALIDATE_DEPLOYMENT`: optional explicit hosted-config validation flag; validation also runs automatically when `AUTO_CREATE_SCHEMA=false`
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
PYTHONPATH=src python -m bidlens.jobs.run_sam_ingest
PYTHONPATH=src python -m bidlens.jobs.run_grants_ingest
PYTHONPATH=src python -m bidlens.jobs.run_daily_snapshots
```

Each command defaults to `--trigger-type scheduled`. For local manual testing, pass:

```bash
--trigger-type manual
```

The Daily Snapshot command also accepts:

```bash
--snapshot-date YYYY-MM-DD
```

Staging assumptions:

```bash
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DBNAME
AUTO_CREATE_SCHEMA=false
ENABLE_INTERNAL_SCHEDULER=false
```

Each standalone job creates one `JobRun` per eligible organization. SAM.gov and Grants.gov jobs also preserve their existing `IngestionRun` records for source-specific ingestion history. Daily Snapshot creates one organization-level `JobRun` with aggregate user counts.

Exit-code policy:

- `0`: all processed organizations ended in `success`, `paused`, or intentional `skipped`.
- Nonzero: one or more organizations ended in `failed` or `partial_success`.

These commands are intended to become Railway cron commands in a later phase. Candidate cron commands:

```bash
PYTHONPATH=src python -m bidlens.jobs.run_sam_ingest
PYTHONPATH=src python -m bidlens.jobs.run_grants_ingest
PYTHONPATH=src python -m bidlens.jobs.run_daily_snapshots
```

Do not run overlapping copies of the same job yet; distributed locking is deferred.

## Platform Operations

Platform Owners can inspect durable operational job history at:

```text
/platform/operations
```

The page is read-only and Platform-only. It uses `JobRun` as the primary source for cross-workspace diagnostics, with filters for organization, job type, status, and date range. Run details show readable aggregate metrics from `details_json` and safe error information. Workspace Admins and Members should not have access.

## Salesforce OAuth POC Setup

BidLens uses the Salesforce OAuth 2.0 Authorization Code flow for the local CRM update proof-of-concept. This avoids the disabled Username-Password flow and lets a Salesforce user authorize BidLens in the browser.

Connected App settings:

- Enable OAuth settings.
- Callback URL: set this to the exact `SALESFORCE_REDIRECT_URI` value used by BidLens.
- OAuth scopes: include `api` and `refresh_token` / `offline_access`.
- Client type: confidential app with a consumer secret.
- The authorizing Salesforce user needs access to query `Opportunity.External_Source_ID_c__c` and update `Opportunity.Intake_Status__c`.

Local authorization:

1. Start BidLens with the Salesforce environment variables configured.
2. While logged in to BidLens, open `/api/salesforce/oauth/start`.
3. Sign in to Salesforce and approve the Connected App.
4. After the callback succeeds, use the temporary "Push to CRM" button. The local POC stores the OAuth token in process memory, so restart requires authorizing again.

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
