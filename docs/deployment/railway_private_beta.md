# Railway Private Beta Deployment

This runbook describes the first private beta Railway web service. It assumes
schema changes are applied with Alembic before the web process starts.

## Web Service

Railway should use the repository `railway.json` for shared build
configuration only. Start commands and deploy health checks are configured per
Railway service so the same repository can run the web application plus
separate cron services.

Custom Start Command:

```bash
PYTHONPATH=src uvicorn bidlens.main:app --host 0.0.0.0 --port "$PORT"
```

Health check:

```text
/health
```

The web process should not run migrations, scheduled pulls, or snapshot jobs at
startup.

## Required Variables

Set these in Railway for the web service:

```bash
DATABASE_URL=postgresql://USER:PASSWORD@HOST:PORT/DB
SECRET_KEY=<strong random value>
AUTO_CREATE_SCHEMA=false
ENABLE_INTERNAL_SCHEDULER=false
SESSION_COOKIE_SECURE=true
```

Railway provides `PORT`; do not hardcode it.

Hosted configuration validation runs automatically when
`AUTO_CREATE_SCHEMA=false`. You may also set
`BIDLENS_VALIDATE_DEPLOYMENT=true` to make that intent explicit.

Optional source and integration variables:

```bash
SAM_API_KEY=<optional>
GRANTS_GOV_API_KEY=<optional>
RESEND_API_KEY=<optional>
DAILY_BRIEF_EMAIL_FROM=<optional>
BIDLENS_APP_BASE_URL=<optional>
SALESFORCE_INSTANCE_URL=<optional>
SALESFORCE_CLIENT_ID=<optional>
SALESFORCE_CLIENT_SECRET=<optional>
SALESFORCE_REDIRECT_URI=<optional>
OPENAI_API_KEY=<optional>
OPENAI_MODEL=<optional>
COMPANY_PROFILE_WEBHOOK_URL=<optional>
```

Configure the Salesforce variables when a private-beta workspace will connect
to Salesforce. `SALESFORCE_INSTANCE_URL` should be the Salesforce My Domain
login URL for the connected app, and `SALESFORCE_REDIRECT_URI` must exactly
match the callback URL configured in Salesforce, for example:

```text
https://<railway-public-domain>/api/salesforce/oauth/callback
```

Salesforce OAuth is workspace-scoped. Workspace admins connect Salesforce from
Workspace Management → Integrations → Salesforce, or from the pre-live Connect
Business Systems setup step. BidLens stores the resulting Salesforce access and
refresh tokens encrypted in PostgreSQL, never in process memory or local files.
Disconnecting Salesforce clears the locally stored encrypted tokens while
preserving existing Salesforce Opportunity IDs, URLs, and sync history.

For the full customer and operator setup procedure, see
[BidLens Salesforce V1 Setup Guide](../integrations/salesforce_setup_guide.md).

Do not upload `.env.railway.local`; it is only for local developer shells.

## Migrations

Run migrations as an explicit Railway command before starting or promoting the
web service:

```bash
PYTHONPATH=src alembic upgrade head
```

Confirm the database is at the current head:

```bash
PYTHONPATH=src alembic current
```

`AUTO_CREATE_SCHEMA` should remain `false` in Railway. If migrations have not
been applied, the app may start but database-backed pages will fail with missing
table errors.

## Initial Verification

After deployment:

1. Open `/health` and confirm HTTP 200 with `{"status":"ok"}`.
2. Visit the login page over HTTPS.
3. Accept or create a beta user through the existing Platform provisioning flow.
4. Confirm authenticated Home loads.
5. Confirm a database-backed page such as Feed or Workspace Management loads.
6. Keep `ENABLE_INTERNAL_SCHEDULER=false` on the web service.

## Operational Jobs

The private beta web service should run without the internal scheduler. Keep
`ENABLE_INTERNAL_SCHEDULER=false` on the Railway web service so the web process
does not create duplicate APScheduler instances.

### Daily SAM.gov Refresh

Use a separate Railway Cron Job for the V1 daily SAM.gov refresh.

Custom Start Command:

```bash
PYTHONPATH=src python -m bidlens.jobs.run_sam_refresh
```

Cron schedule:

```text
0 12 * * *
```

This runs at 12:00 UTC, which is approximately 5:00 AM Phoenix time. Arizona
does not observe daylight saving time.

The cron service must use the same production database and source credentials
as the web service, including:

```bash
DATABASE_URL=postgresql://USER:PASSWORD@HOST:PORT/DB
SECRET_KEY=<same production secret key policy as web service>
SAM_API_KEY=<SAM.gov API key>
AUTO_CREATE_SCHEMA=false
ENABLE_INTERNAL_SCHEDULER=false
SESSION_COOKIE_SECURE=true
```

Do not include secrets directly in the command. Store them as Railway
environment variables.

Manual validation command:

```bash
PYTHONPATH=src python -m bidlens.jobs.run_sam_refresh
```

The command runs the scheduled SAM operational job once, processes eligible
live organizations, records normal JobRun and pull-history records, then exits.
It is safe to invoke once daily because BidLens uses the existing SAM ingestion
deduplication and source-record upsert behavior. Individual organization
failures are recorded and isolated; a job-level startup/database failure returns
a nonzero exit code.

### Daily Grants.gov Refresh

Use a separate Railway Cron Job for the V1 daily Grants.gov refresh.

Custom Start Command:

```bash
PYTHONPATH=src python -m bidlens.jobs.run_grants_refresh
```

Cron schedule:

```text
15 12 * * *
```

The cron service must use the same production database and source credentials
as the web service, including:

```bash
DATABASE_URL=postgresql://USER:PASSWORD@HOST:PORT/DB
SECRET_KEY=<same production secret key policy as web service>
GRANTS_GOV_API_KEY=<Grants.gov API key>
AUTO_CREATE_SCHEMA=false
ENABLE_INTERNAL_SCHEDULER=false
SESSION_COOKIE_SECURE=true
```

`GRANTS_GOV_SEARCH_URL` is optional because BidLens provides the Grants.gov
Search2 API default. Do not include secrets directly in the command. Store them
as Railway environment variables.

Manual validation command:

```bash
PYTHONPATH=src python -m bidlens.jobs.run_grants_refresh
```

The command runs the scheduled Grants.gov operational job once, processes
eligible live organizations, records normal JobRun and ingestion-history
records, then exits. Individual organization failures are recorded and isolated;
a job-level startup/database failure returns a nonzero exit code.

### Daily Snapshots

Use a separate Railway Cron Job to precompute the in-app Daily Brief snapshots.
This job must run before Daily Brief Email delivery.

Custom Start Command:

```bash
PYTHONPATH=src python -m bidlens.jobs.run_daily_snapshots
```

Cron schedule:

```text
0 13 * * *
```

The snapshot cron service must use the same production database and application
settings as the web service:

```bash
DATABASE_URL=postgresql://USER:PASSWORD@HOST:PORT/DB
SECRET_KEY=<same production secret key policy as web service>
AUTO_CREATE_SCHEMA=false
ENABLE_INTERNAL_SCHEDULER=false
SESSION_COOKIE_SECURE=true
```

Manual validation command:

```bash
PYTHONPATH=src python -m bidlens.jobs.run_daily_snapshots --trigger-type manual
```

### Daily Brief Emails

Use a separate Railway Cron Job to send the V1 low-noise Daily Brief email after
Daily Snapshots have been generated.

Custom Start Command:

```bash
PYTHONPATH=src python -m bidlens.jobs.run_daily_brief_emails
```

Cron schedule:

```text
15 13 * * *
```

The email cron service must use the same production database and application
settings as the web service plus email-delivery credentials:

```bash
DATABASE_URL=postgresql://USER:PASSWORD@HOST:PORT/DB
SECRET_KEY=<same production secret key policy as web service>
AUTO_CREATE_SCHEMA=false
ENABLE_INTERNAL_SCHEDULER=false
SESSION_COOKIE_SECURE=true
RESEND_API_KEY=<Resend API key>
DAILY_BRIEF_EMAIL_FROM=<verified sender address>
BIDLENS_APP_BASE_URL=https://<railway-public-domain>
```

The email job sends one Daily Brief per eligible user and snapshot date. It uses
the stored Daily Snapshot for that date, includes only new Feed opportunity
content, records durable delivery history, skips previously successful sends on
rerun, and allows failed deliveries to be retried.

Manual validation command:

```bash
PYTHONPATH=src python -m bidlens.jobs.run_daily_brief_emails --trigger-type manual
```

### Other Operational Jobs

Other standalone jobs can be run by separate Railway cron or worker services:

```bash
PYTHONPATH=src python -m bidlens.jobs.run_sam_ingest
PYTHONPATH=src python -m bidlens.jobs.run_grants_ingest
```

Those jobs are intentionally not part of the web startup command.
