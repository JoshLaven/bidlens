import os
import re
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


_SENSITIVE_TEXT_PATTERNS = (
    (re.compile(r"([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^@\s/]+)@", re.IGNORECASE), r"\1[redacted]:[redacted]@"),
    (re.compile(r"(?i)(access[_-]?key(?:_id)?|secret(?:_access)?[_-]?key|password|token|signature|x-amz-signature)(\s*[=:]\s*)[^\s,;&]+"), r"\1\2[redacted]"),
    (re.compile(r"(?i)([?&](?:x-amz-credential|x-amz-signature|x-amz-security-token|signature|token)=)[^&#\s]+"), r"\1[redacted]"),
)


def redact_sensitive_text(value: object) -> str:
    """Redact common credentials and signed-URL values from diagnostic text."""

    redacted = str(value)
    for pattern, replacement in _SENSITIVE_TEXT_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def database_target_summary(value: str) -> str:
    """Describe a database target without returning credentials or query values."""

    try:
        url = make_url(value)
    except Exception:
        return "provider=unknown host=unknown port=unknown database=unknown"
    return (
        f"provider={url.get_backend_name() or 'unknown'} "
        f"host={url.host or 'local'} port={url.port or 'default'} "
        f"database={url.database or 'unknown'}"
    )


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
GUTS_ENABLED = _env_bool("GUTS_ENABLED", False)
GUTS_AI_PROVIDER = os.getenv("GUTS_AI_PROVIDER", "openai").strip().lower()
GUTS_AI_MODEL = os.getenv("GUTS_AI_MODEL") or OPENAI_MODEL
GUTS_PROMPT_VERSION = os.getenv("GUTS_PROMPT_VERSION", "guts-v6")
GUTS_MANIFEST_VERSION = os.getenv("GUTS_MANIFEST_VERSION", "guts-manifest-v1")
GUTS_OUTPUT_SCHEMA_VERSION = os.getenv("GUTS_OUTPUT_SCHEMA_VERSION", "guts-output-v1")
GUTS_TIMEOUT_SECONDS = float(os.getenv("GUTS_TIMEOUT_SECONDS", "45"))
GUTS_MAX_OUTPUT_TOKENS = int(os.getenv("GUTS_MAX_OUTPUT_TOKENS", "2400"))
GUTS_MAX_RETRIES = int(os.getenv("GUTS_MAX_RETRIES", "1"))
GUTS_STALE_ATTEMPT_SECONDS = int(os.getenv("GUTS_STALE_ATTEMPT_SECONDS", "900"))
GUTS_MAX_TOTAL_INPUT_CHARS = int(os.getenv("GUTS_MAX_TOTAL_INPUT_CHARS", "100000"))
GUTS_MAX_NOTES = int(os.getenv("GUTS_MAX_NOTES", "20"))
GUTS_MAX_NOTE_CHARS = int(os.getenv("GUTS_MAX_NOTE_CHARS", "3000"))
GUTS_MAX_TOTAL_NOTE_CHARS = int(os.getenv("GUTS_MAX_TOTAL_NOTE_CHARS", "20000"))
GUTS_MAX_MESSAGES = int(os.getenv("GUTS_MAX_MESSAGES", "50"))
GUTS_MAX_MESSAGE_CHARS = int(os.getenv("GUTS_MAX_MESSAGE_CHARS", "5000"))
GUTS_MAX_TOTAL_COMMUNICATION_CHARS = int(
    os.getenv("GUTS_MAX_TOTAL_COMMUNICATION_CHARS", "60000")
)
GUTS_MAX_HISTORY_EVENTS = int(os.getenv("GUTS_MAX_HISTORY_EVENTS", "20"))
GUTS_MAX_TOTAL_HISTORY_CHARS = int(os.getenv("GUTS_MAX_TOTAL_HISTORY_CHARS", "10000"))
GUTS_MAX_OFFICIAL_DOCUMENTS = int(os.getenv("GUTS_MAX_OFFICIAL_DOCUMENTS", "5"))
GUTS_MAX_OFFICIAL_DOC_CHARS = int(os.getenv("GUTS_MAX_OFFICIAL_DOC_CHARS", "30000"))
GUTS_MAX_TOTAL_OFFICIAL_CHARS = int(os.getenv("GUTS_MAX_TOTAL_OFFICIAL_CHARS", "60000"))
GUTS_MAX_SUMMARY_STATEMENTS = int(os.getenv("GUTS_MAX_SUMMARY_STATEMENTS", "5"))
GUTS_MAX_SECTIONS = int(os.getenv("GUTS_MAX_SECTIONS", "5"))
GUTS_MAX_STATEMENTS_PER_SECTION = int(os.getenv("GUTS_MAX_STATEMENTS_PER_SECTION", "6"))
GUTS_MAX_STATEMENT_CHARS = int(os.getenv("GUTS_MAX_STATEMENT_CHARS", "600"))
GUTS_MAX_TOTAL_OUTPUT_CHARS = int(os.getenv("GUTS_MAX_TOTAL_OUTPUT_CHARS", "5000"))
AI_SUMMARY_PROVIDER = os.getenv("AI_SUMMARY_PROVIDER", "openai")
AI_SUMMARY_API_KEY = os.getenv("AI_SUMMARY_API_KEY") or OPENAI_API_KEY
AI_SUMMARY_MODEL = os.getenv("AI_SUMMARY_MODEL") or OPENAI_MODEL
AI_SUMMARY_MAX_INPUT_CHARS = int(os.getenv("AI_SUMMARY_MAX_INPUT_CHARS", "30000"))
AI_SUMMARY_MAX_OUTPUT_TOKENS = int(os.getenv("AI_SUMMARY_MAX_OUTPUT_TOKENS", "900"))
AI_SUMMARY_TEMPERATURE = float(os.getenv("AI_SUMMARY_TEMPERATURE", "0"))
AI_SUMMARY_TIMEOUT_SECONDS = float(os.getenv("AI_SUMMARY_TIMEOUT_SECONDS", "30"))
AI_SUMMARY_MAX_RETRIES = int(os.getenv("AI_SUMMARY_MAX_RETRIES", "0"))
AI_SUMMARY_BASE_URL = os.getenv("AI_SUMMARY_BASE_URL")
COMPANY_PROFILE_WEBHOOK_URL = os.getenv("COMPANY_PROFILE_WEBHOOK_URL")
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
DAILY_BRIEF_EMAIL_FROM = os.getenv("DAILY_BRIEF_EMAIL_FROM")
BIDLENS_APP_BASE_URL = os.getenv("BIDLENS_APP_BASE_URL")
SALESFORCE_INSTANCE_URL = os.getenv("SALESFORCE_INSTANCE_URL")
SALESFORCE_CLIENT_ID = os.getenv("SALESFORCE_CLIENT_ID")
SALESFORCE_CLIENT_SECRET = os.getenv("SALESFORCE_CLIENT_SECRET")
SALESFORCE_REDIRECT_URI = os.getenv("SALESFORCE_REDIRECT_URI")
MICROSOFT_CLIENT_ID = os.getenv("MICROSOFT_CLIENT_ID")
MICROSOFT_CLIENT_SECRET = os.getenv("MICROSOFT_CLIENT_SECRET")
MICROSOFT_REDIRECT_URI = os.getenv("MICROSOFT_REDIRECT_URI")
MICROSOFT_TENANT_ID = os.getenv("MICROSOFT_TENANT_ID", "common")
SOURCE_MATERIAL_STORAGE_BACKEND = os.getenv("SOURCE_MATERIAL_STORAGE_BACKEND", "local").strip().lower()
SOURCE_MATERIAL_LOCAL_ROOT = Path(
    os.getenv("SOURCE_MATERIAL_LOCAL_ROOT", str(BASE_DIR / ".bidlens" / "source-materials"))
).expanduser()
SOURCE_MATERIAL_S3_BUCKET = os.getenv("SOURCE_MATERIAL_S3_BUCKET", "").strip()
SOURCE_MATERIAL_S3_ENDPOINT_URL = os.getenv("SOURCE_MATERIAL_S3_ENDPOINT_URL", "").strip() or None
SOURCE_MATERIAL_S3_REGION = os.getenv("SOURCE_MATERIAL_S3_REGION", "us-east-1").strip()
SOURCE_MATERIAL_S3_ACCESS_KEY_ID = os.getenv("SOURCE_MATERIAL_S3_ACCESS_KEY_ID", "").strip()
SOURCE_MATERIAL_S3_SECRET_ACCESS_KEY = os.getenv("SOURCE_MATERIAL_S3_SECRET_ACCESS_KEY", "").strip()
SOURCE_MATERIAL_S3_PATH_PREFIX = os.getenv("SOURCE_MATERIAL_S3_PATH_PREFIX", "bidlens/source-materials").strip()
SOURCE_MATERIAL_S3_USE_SSL = _env_bool("SOURCE_MATERIAL_S3_USE_SSL", True)
SOURCE_MATERIAL_MAX_BYTES = int(os.getenv("SOURCE_MATERIAL_MAX_BYTES", str(25 * 1024 * 1024)))
INTAKE_DOCUMENT_MAX_TEXT_CHARS = int(os.getenv("INTAKE_DOCUMENT_MAX_TEXT_CHARS", "60000"))
INTAKE_DOCUMENT_MAX_PDF_PAGES = int(os.getenv("INTAKE_DOCUMENT_MAX_PDF_PAGES", "50"))
INTAKE_EXTRACTION_MODEL = os.getenv("INTAKE_EXTRACTION_MODEL") or OPENAI_MODEL
INTAKE_EXTRACTION_TIMEOUT_SECONDS = float(os.getenv("INTAKE_EXTRACTION_TIMEOUT_SECONDS", "30"))
INTAKE_EXTRACTION_MAX_OUTPUT_TOKENS = int(os.getenv("INTAKE_EXTRACTION_MAX_OUTPUT_TOKENS", "1800"))
INTAKE_EMAIL_MAX_BODY_CHARS = int(os.getenv("INTAKE_EMAIL_MAX_BODY_CHARS", "20000"))
INTAKE_EMAIL_MAX_ATTACHMENTS = int(os.getenv("INTAKE_EMAIL_MAX_ATTACHMENTS", "10"))
INTAKE_EMAIL_MAX_ATTACHMENT_BYTES = int(os.getenv("INTAKE_EMAIL_MAX_ATTACHMENT_BYTES", str(15 * 1024 * 1024)))
INTAKE_EMAIL_MAX_TOTAL_ATTACHMENT_BYTES = int(os.getenv("INTAKE_EMAIL_MAX_TOTAL_ATTACHMENT_BYTES", str(20 * 1024 * 1024)))
INTAKE_EMAIL_MAX_EXTRACTION_CHARS = int(os.getenv("INTAKE_EMAIL_MAX_EXTRACTION_CHARS", "80000"))
INTAKE_EMAIL_MAX_PARSING_SECONDS = float(os.getenv("INTAKE_EMAIL_MAX_PARSING_SECONDS", "15"))


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
    source_material_storage_backend: str | None = None,
    source_material_s3_bucket: str | None = None,
    source_material_s3_endpoint_url: str | None = None,
    source_material_s3_access_key_id: str | None = None,
    source_material_s3_secret_access_key: str | None = None,
    source_material_s3_use_ssl: bool | None = None,
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
    if source_material_storage_backend is None:
        source_material_storage_backend = SOURCE_MATERIAL_STORAGE_BACKEND
    if source_material_s3_bucket is None:
        source_material_s3_bucket = SOURCE_MATERIAL_S3_BUCKET
    if source_material_s3_endpoint_url is None:
        source_material_s3_endpoint_url = SOURCE_MATERIAL_S3_ENDPOINT_URL
    if source_material_s3_access_key_id is None:
        source_material_s3_access_key_id = SOURCE_MATERIAL_S3_ACCESS_KEY_ID
    if source_material_s3_secret_access_key is None:
        source_material_s3_secret_access_key = SOURCE_MATERIAL_S3_SECRET_ACCESS_KEY
    if source_material_s3_use_ssl is None:
        source_material_s3_use_ssl = SOURCE_MATERIAL_S3_USE_SSL

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

    if source_material_storage_backend != "s3":
        errors.append("SOURCE_MATERIAL_STORAGE_BACKEND must be s3 for hosted deployment.")
    else:
        if not source_material_s3_bucket:
            errors.append("SOURCE_MATERIAL_S3_BUCKET is required for hosted deployment.")
        if not source_material_s3_access_key_id:
            errors.append("SOURCE_MATERIAL_S3_ACCESS_KEY_ID is required for hosted deployment.")
        if not source_material_s3_secret_access_key:
            errors.append("SOURCE_MATERIAL_S3_SECRET_ACCESS_KEY is required for hosted deployment.")
        if not source_material_s3_use_ssl:
            errors.append("SOURCE_MATERIAL_S3_USE_SSL must be true for hosted deployment.")
        if source_material_s3_endpoint_url and not source_material_s3_endpoint_url.lower().startswith("https://"):
            errors.append("SOURCE_MATERIAL_S3_ENDPOINT_URL must use HTTPS for hosted deployment.")

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
    source_material_storage_backend: str | None = None,
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
    if source_material_storage_backend is None:
        source_material_storage_backend = SOURCE_MATERIAL_STORAGE_BACKEND

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
        f"Source-material storage backend: {source_material_storage_backend or '<unset>'}",
    ]
