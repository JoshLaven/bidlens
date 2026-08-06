from __future__ import annotations

from collections.abc import Iterable, Mapping
import csv
from dataclasses import dataclass
from functools import lru_cache
import logging
from pathlib import Path
import re
from types import MappingProxyType
import unicodedata

from .. import config
from .agency_display import agency_presentation


_AMPERSAND = re.compile(r"\s*&\s*")
_UNITED_STATES = re.compile(r"(?<![\w])united\s+states(?![\w])", re.IGNORECASE)
_DOTTED_US = re.compile(r"(?<![\w])u\s*\.\s*s\s*\.?(?![\w])", re.IGNORECASE)
_PLAIN_US = re.compile(r"(?<![\w])us(?![\w])", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")
DEFAULT_ACCOUNT_ALIAS_FILE = Path(__file__).resolve().parents[1] / "data" / "account_aliases.csv"
REQUIRED_ALIAS_COLUMNS = frozenset({"Alias", "Display"})
logger = logging.getLogger(__name__)


class AccountAliasConflictError(ValueError):
    """Raised when formatting-equivalent aliases map to different accounts."""


class AccountAliasConfigurationError(RuntimeError):
    """Raised when the configured semantic alias source cannot be used safely."""


@dataclass(frozen=True)
class AccountAlias:
    account: str
    display_name: str


def normalize_account_lookup_key(value: str | None) -> str:
    """Return a formatting-insensitive key for exact alias lookup.

    This intentionally performs no semantic expansion beyond the explicitly
    equivalent United States spellings. Acronyms, misspellings, renamed
    agencies, parent/sub-agency relationships, and singular/plural forms must
    remain explicit mappings in the alias source.
    """

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = _AMPERSAND.sub(" and ", text)
    text = _UNITED_STATES.sub(" united states ", text)
    text = _DOTTED_US.sub(" united states ", text)
    text = _PLAIN_US.sub(" united states ", text)
    text = "".join(" " if unicodedata.category(char).startswith("P") else char for char in text)
    return _WHITESPACE.sub(" ", text).strip()


def build_account_alias_lookup(
    aliases: Iterable[AccountAlias | Mapping[str, object]],
) -> dict[str, str]:
    """Build a normalized alias lookup without changing semantic mappings."""

    lookup: dict[str, str] = {}
    source_accounts: dict[str, str] = {}
    for row in aliases:
        if isinstance(row, AccountAlias):
            account = row.account
            display_name = row.display_name
        else:
            account = str(row.get("Account") or row.get("Alias") or "").strip()
            display_name = str(row.get("Display Name") or row.get("Display") or "").strip()

        key = normalize_account_lookup_key(account)
        if not key or not display_name:
            continue

        existing = lookup.get(key)
        if existing is not None and existing != display_name:
            source = source_accounts[key]
            raise AccountAliasConflictError(
                "Formatting-equivalent account aliases map to different display names: "
                f"{source!r} and {account!r}."
            )
        lookup[key] = display_name
        source_accounts.setdefault(key, account)
    return lookup


def match_account_alias(account_name: str | None, alias_lookup: Mapping[str, str]) -> str | None:
    """Resolve an incoming account name through a normalized semantic lookup."""

    key = normalize_account_lookup_key(account_name)
    return alias_lookup.get(key) if key else None


def configured_account_alias_file() -> Path:
    """Resolve an explicit alias path before the bundled repository default."""

    return config.ACCOUNT_ALIAS_FILE_PATH or DEFAULT_ACCOUNT_ALIAS_FILE


@lru_cache(maxsize=4)
def _load_account_alias_lookup(path_value: str) -> Mapping[str, str]:
    path = Path(path_value)
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            columns = set(reader.fieldnames or [])
            if not REQUIRED_ALIAS_COLUMNS.issubset(columns):
                raise AccountAliasConfigurationError(
                    "Account alias data must contain Alias and Display columns."
                )
            rows = list(reader)
        lookup = build_account_alias_lookup(rows)
    except (OSError, UnicodeError, csv.Error, AccountAliasConflictError, AccountAliasConfigurationError) as exc:
        logger.error(
            "account_alias_configuration_failed file=%s error_type=%s",
            path.name or "<unknown>",
            type(exc).__name__,
        )
        if isinstance(exc, AccountAliasConfigurationError):
            raise
        raise AccountAliasConfigurationError("Account alias data could not be loaded safely.") from exc
    return MappingProxyType(lookup)


def get_account_alias_lookup() -> Mapping[str, str]:
    """Return the process-cached semantic alias lookup."""

    return _load_account_alias_lookup(str(configured_account_alias_file().resolve()))


def clear_account_alias_cache() -> None:
    """Clear cached configuration for tests or an explicit configuration revision."""

    _load_account_alias_lookup.cache_clear()


def resolve_account_display_name(
    raw_account: str | None,
    *,
    alias_lookup: Mapping[str, str] | None = None,
) -> str:
    """Resolve semantic aliases first, then use legacy presentation cleanup."""

    lookup = alias_lookup if alias_lookup is not None else get_account_alias_lookup()
    resolved = match_account_alias(raw_account, lookup)
    return resolved or agency_presentation(raw_account).display
