import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[2]
DOTENV_PATH = BASE_DIR / ".env"

# Load the repo-local .env explicitly so app code and maintenance scripts
# resolve the same environment file regardless of current working directory.
# `override=True` ensures a stale exported shell value does not beat the
# project-local SAM_API_KEY after a rotation.
load_dotenv(DOTENV_PATH, override=True)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./bidlens.db")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
SESSION_COOKIE_NAME = "bidlens_session"
SAM_API_KEY = os.getenv("SAM_API_KEY")
GRANTS_GOV_API_KEY = os.getenv("GRANTS_GOV_API_KEY")
GRANTS_GOV_SEARCH_URL = os.getenv("GRANTS_GOV_SEARCH_URL", "https://api.grants.gov/v1/api/search2")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
COMPANY_PROFILE_WEBHOOK_URL = os.getenv("COMPANY_PROFILE_WEBHOOK_URL")
SALESFORCE_INSTANCE_URL = os.getenv("SALESFORCE_INSTANCE_URL")
SALESFORCE_CLIENT_ID = os.getenv("SALESFORCE_CLIENT_ID")
SALESFORCE_CLIENT_SECRET = os.getenv("SALESFORCE_CLIENT_SECRET")
SALESFORCE_REDIRECT_URI = os.getenv("SALESFORCE_REDIRECT_URI")
