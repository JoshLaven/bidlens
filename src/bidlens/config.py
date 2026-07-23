import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import make_url


BASE_DIR = Path(__file__).resolve().parents[2]
DOTENV_PATH = BASE_DIR / ".env"
DEFAULT_SECRET_KEY = "dev-secret-key-change-in-production"

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


RAW_DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL = normalize_database_url(RAW_DATABASE_URL)
DATABASE_SCHEME = database_url_scheme(DATABASE_URL)
SECRET_KEY = os.getenv("SECRET_KEY", DEFAULT_SECRET_KEY)
SESSION_COOKIE_NAME = "bidlens_session"
SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", False)
ENABLE_INTERNAL_SCHEDULER = _env_bool("ENABLE_INTERNAL_SCHEDULER", False)
AUTO_CREATE_SCHEMA = _env_bool("AUTO_CREATE_SCHEMA", True)
VALIDATE_DEPLOYMENT_CONFIG = _env_bool("BIDLENS_VALIDATE_DEPLOYMENT", False)
SAM_API_KEY = os.getenv("SAM_API_KEY")
GRANTS_GOV_API_KEY = os.getenv("GRANTS_GOV_API_KEY")
GRANTS_GOV_SEARCH_URL = os.getenv("GRANTS_GOV_SEARCH_URL", "https://api.grants.gov/v1/api/search2")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
COMPANY_PROFILE_WEBHOOK_URL = os.getenv("COMPANY_PROFILE_WEBHOOK_URL")
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
DAILY_BRIEF_EMAIL_FROM = os.getenv("DAILY_BRIEF_EMAIL_FROM")
BIDLENS_APP_BASE_URL = os.getenv("BIDLENS_APP_BASE_URL")
SALESFORCE_INSTANCE_URL = os.getenv("SALESFORCE_INSTANCE_URL")
SALESFORCE_CLIENT_ID = os.getenv("SALESFORCE_CLIENT_ID")
SALESFORCE_CLIENT_SECRET = os.getenv("SALESFORCE_CLIENT_SECRET")
SALESFORCE_REDIRECT_URI = os.getenv("SALESFORCE_REDIRECT_URI")


class DeploymentConfigError(RuntimeError):
    """Raised when hosted deployment settings are unsafe or incomplete."""


def deployment_validation_enabled(
    *,
    auto_create_schema: bool | None = None,
    explicit_validate: bool | None = None,
) -> bool:
    """Enable hosted validation for production-style startup configurations."""

    if auto_create_schema is None:
        auto_create_schema = AUTO_CREATE_SCHEMA
    if explicit_validate is None:
        explicit_validate = VALIDATE_DEPLOYMENT_CONFIG
    return bool(explicit_validate or not auto_create_schema)


def validate_deployment_config(
    *,
    raw_database_url: str | None = None,
    database_url: str | None = None,
    database_scheme: str | None = None,
    secret_key: str | None = None,
    session_cookie_secure: bool | None = None,
    auto_create_schema: bool | None = None,
    enable_internal_scheduler: bool | None = None,
    explicit_validate: bool | None = None,
) -> None:
    """Validate hosted web-process settings without exposing secret values."""

    if raw_database_url is None:
        raw_database_url = RAW_DATABASE_URL
    if database_url is None:
        database_url = DATABASE_URL
    if database_scheme is None:
        database_scheme = DATABASE_SCHEME
    if secret_key is None:
        secret_key = SECRET_KEY
    if session_cookie_secure is None:
        session_cookie_secure = SESSION_COOKIE_SECURE
    if auto_create_schema is None:
        auto_create_schema = AUTO_CREATE_SCHEMA
    if enable_internal_scheduler is None:
        enable_internal_scheduler = ENABLE_INTERNAL_SCHEDULER

    if not deployment_validation_enabled(
        auto_create_schema=auto_create_schema,
        explicit_validate=explicit_validate,
    ):
        return

    errors: list[str] = []
    if not (raw_database_url or "").strip():
        errors.append("DATABASE_URL is required for hosted deployment.")
    elif database_scheme != "postgresql":
        errors.append("DATABASE_URL must use PostgreSQL for hosted deployment.")

    if not (secret_key or "").strip() or secret_key == DEFAULT_SECRET_KEY:
        errors.append("SECRET_KEY must be explicitly set to a non-development value.")

    if not session_cookie_secure:
        errors.append("SESSION_COOKIE_SECURE must be true for hosted HTTPS deployment.")

    if auto_create_schema:
        errors.append("AUTO_CREATE_SCHEMA must be false for hosted deployment.")

    if enable_internal_scheduler:
        errors.append("ENABLE_INTERNAL_SCHEDULER must be false for the Railway web service.")

    if errors:
        raise DeploymentConfigError(
            "Hosted deployment configuration is invalid:\n- " + "\n- ".join(errors)
        )


def startup_diagnostics(
    *,
    database_scheme: str | None = None,
    auto_create_schema: bool | None = None,
    enable_internal_scheduler: bool | None = None,
    session_cookie_secure: bool | None = None,
    explicit_validate: bool | None = None,
) -> list[str]:
    """Return non-secret startup facts for deployment troubleshooting."""

    if database_scheme is None:
        database_scheme = DATABASE_SCHEME
    if auto_create_schema is None:
        auto_create_schema = AUTO_CREATE_SCHEMA
    if enable_internal_scheduler is None:
        enable_internal_scheduler = ENABLE_INTERNAL_SCHEDULER
    if session_cookie_secure is None:
        session_cookie_secure = SESSION_COOKIE_SECURE

    validation_enabled = deployment_validation_enabled(
        auto_create_schema=auto_create_schema,
        explicit_validate=explicit_validate,
    )

    return [
        f"Database backend: {database_scheme}",
        f"Auto-create schema: {'enabled' if auto_create_schema else 'disabled'}",
        f"Internal scheduler: {'enabled' if enable_internal_scheduler else 'disabled'}",
        f"Secure session cookie: {'enabled' if session_cookie_secure else 'disabled'}",
        f"Hosted deployment validation: {'enabled' if validation_enabled else 'disabled'}",
    ]
