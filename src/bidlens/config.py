import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import make_url


BASE_DIR = Path(__file__).resolve().parents[2]
DOTENV_PATH = BASE_DIR / ".env"

# Load the repo-local .env explicitly so app code and maintenance scripts
# resolve the same environment file regardless of current working directory.
# Do not override already-exported environment variables; hosted staging should
# treat platform-provided env vars as authoritative.
load_dotenv(DOTENV_PATH, override=False)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def normalize_database_url(value: str | None) -> str:
    database_url = (value or "sqlite:///./bidlens.db").strip()
    if database_url.startswith("postgres://"):
        return f"postgresql://{database_url.removeprefix('postgres://')}"
    return database_url


def database_url_scheme(value: str) -> str:
    try:
        return make_url(value).get_backend_name()
    except Exception:
        return value.split(":", 1)[0].lower()


def safe_database_url(value: str) -> str:
    try:
        return make_url(value).render_as_string(hide_password=True)
    except Exception:
        return "<unparseable database url>"


DATABASE_URL = normalize_database_url(os.getenv("DATABASE_URL"))
DATABASE_SCHEME = database_url_scheme(DATABASE_URL)
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
SESSION_COOKIE_NAME = "bidlens_session"
SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", False)
ENABLE_INTERNAL_SCHEDULER = _env_bool("ENABLE_INTERNAL_SCHEDULER", False)
AUTO_CREATE_SCHEMA = _env_bool("AUTO_CREATE_SCHEMA", True)
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
