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

## Environment Variables

- `SAM_API_KEY`: SAM.gov API key used for opportunity pulls and notice description fetches
- `DATABASE_URL`: PostgreSQL connection string
- `SECRET_KEY`: Session encryption key (defaults to dev key)
- `SALESFORCE_INSTANCE_URL`: Salesforce My Domain URL, for example `https://your-domain.my.salesforce.com`
- `SALESFORCE_CLIENT_ID`: Salesforce Connected App consumer key
- `SALESFORCE_CLIENT_SECRET`: Salesforce Connected App consumer secret
- `SALESFORCE_REDIRECT_URI`: OAuth callback URL, for example `http://127.0.0.1:8000/api/salesforce/oauth/callback`

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
