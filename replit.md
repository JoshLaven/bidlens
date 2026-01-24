# BidLens

## Overview
BidLens is a web application for small businesses to triage SAM.gov Contract Opportunities. Built with Python FastAPI backend and Jinja2 server-rendered templates.

## Project Structure
```
src/bidlens/
├── main.py          # FastAPI entry point
├── config.py        # Configuration (DATABASE_URL, SECRET_KEY)
├── database.py      # SQLAlchemy setup
├── models.py        # Opportunity, User, UserOpportunity models
├── auth.py          # Session-based authentication
├── routes/
│   ├── auth.py      # Login/logout routes
│   └── opportunities.py  # Feed, detail, saved, calendar routes
├── templates/       # Jinja2 HTML templates
└── static/css/      # Stylesheets
seed.py              # Database seeding script
```

## Key Features
- Feed page with Solicitations/RFIs tabs
- Opportunity detail page with save/notes/deadline
- My Bids page for tracking saved opportunities
- Calendar view for saved solicitations by deadline

## Running
- Server: `uvicorn src.bidlens.main:app --host 0.0.0.0 --port 5000`
- Seed data: `python seed.py`

## Database
Uses PostgreSQL via SQLAlchemy with models:
- `opportunities` - SAM.gov contract opportunities
- `users` - User accounts with is_paid flag
- `user_opportunities` - User-specific state (saved, notes, deadline)

## Authentication
Dev mode: Enter email to create session (no password). Structure ready for magic link implementation.
