import datetime as dt
import time
import threading
import logging
import json
import re
from html import unescape
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
import requests
from typing import Any, Dict

from .config import SAM_API_KEY

SAM_BASE = "https://api.sam.gov/opportunities/v2/search"
logger = logging.getLogger(__name__)
_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0
MIN_REQUEST_INTERVAL_SECONDS = 1.0
MAX_RATE_LIMIT_WAIT_SECONDS = 30.0
TRANSIENT_SAM_STATUS_CODES = {500, 502, 503, 504}


class SamRateLimitError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        retry_after: str | None = None,
    ):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.retry_after = retry_after


class SamTemporaryUnavailableError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _is_url_like(value: str | None) -> bool:
    if value is None:
        return False
    s = str(value).strip().lower()
    return s.startswith("http://") or s.startswith("https://") or s.startswith("www.")

def _mmddyyyy(d: dt.date) -> str:
    return d.strftime("%m/%d/%Y")


def _throttle_requests() -> None:
    global _LAST_REQUEST_AT
    with _REQUEST_LOCK:
        now = time.monotonic()
        wait_s = max(0.0, MIN_REQUEST_INTERVAL_SECONDS - (now - _LAST_REQUEST_AT))
        if wait_s > 0:
            time.sleep(wait_s)
        _LAST_REQUEST_AT = time.monotonic()


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None

    try:
        return max(0.0, float(value))
    except ValueError:
        pass

    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None

    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=dt.timezone.utc)

    now = dt.datetime.now(dt.timezone.utc)
    return max(0.0, (retry_at - now).total_seconds())


def _parse_retry_at(value: str | None) -> float | None:
    if not value:
        return None

    for fmt in ("%Y-%b-%d %H:%M:%S%z UTC", "%Y-%b-%d %H:%M:%S %Z"):
        try:
            retry_at = dt.datetime.strptime(value, fmt)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=dt.timezone.utc)
            now = dt.datetime.now(dt.timezone.utc)
            return max(0.0, (retry_at - now).total_seconds())
        except ValueError:
            continue

    return _parse_retry_after(value)


def _with_api_key(url: str, api_key: str) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("api_key", api_key)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _normalize_description_payload(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("description", "noticeDesc", "noticeDescription", "body", "content"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in payload.values():
            normalized = _normalize_description_payload(value)
            if normalized:
                return normalized
        return None

    if isinstance(payload, list):
        for item in payload:
            normalized = _normalize_description_payload(item)
            if normalized:
                return normalized
        return None

    if isinstance(payload, str):
        text = payload.strip()
        if not text or _is_url_like(text):
            return None
        return text

    return None


def _extract_sam_error_text(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("error", "errorMessage", "message", "description"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("errors", "messages"):
            value = payload.get(key)
            if isinstance(value, list):
                parts = [str(item).strip() for item in value if str(item).strip()]
                if parts:
                    return "; ".join(parts)
        return None

    if isinstance(payload, list):
        parts = [str(item).strip() for item in payload if str(item).strip()]
        return "; ".join(parts) if parts else None

    if isinstance(payload, str):
        text = payload.strip()
        return text or None

    return None


def _looks_like_sam_runtime_error(payload: Any) -> bool:
    message = _extract_sam_error_text(payload)
    if not message:
        return False

    normalized = message.lower()
    markers = (
        "runtime error",
        "internal server error",
        "temporarily unavailable",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
    )
    return any(marker in normalized for marker in markers)


def _extract_retry_after_seconds(resp: requests.Response) -> float | None:
    retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
    if retry_after is not None:
        return retry_after

    try:
        payload = resp.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        for key in ("nextAccessTime", "retryAfter", "retry_after", "next_access_time"):
            retry_after = _parse_retry_at(payload.get(key))
            if retry_after is not None:
                return retry_after

    return None


def _extract_retry_after_value(resp: requests.Response) -> str | None:
    header_value = resp.headers.get("Retry-After")
    if isinstance(header_value, str) and header_value.strip():
        return header_value.strip()

    try:
        payload = resp.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        for key in ("nextAccessTime", "retryAfter", "retry_after", "next_access_time"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return None


def _rate_limit_message(resp: requests.Response, fallback_message: str) -> str:
    retry_after_seconds = _extract_retry_after_seconds(resp)
    try:
        payload = resp.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        message = payload.get("description") or payload.get("message")
        if isinstance(message, str) and message.strip():
            if retry_after_seconds is not None:
                return f"{message.strip()} Retry after about {int(round(retry_after_seconds))} seconds."
            return message.strip()

    if retry_after_seconds is not None:
        return f"{fallback_message} Retry after about {int(round(retry_after_seconds))} seconds."
    return fallback_message


def _clean_extracted_text(text: str | None) -> str | None:
    if not text:
        return None

    cleaned = unescape(text)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None

    lowered = cleaned.lower()
    generic_markers = (
        "sam acquisition 360",
        "javascript",
        "enable javascript",
        "sign in",
    )
    if any(marker in lowered for marker in generic_markers):
        return None
    if _is_url_like(cleaned):
        return None

    return cleaned


def fetch_sam_page_description(sam_url: str) -> str | None:
    if not _is_url_like(sam_url):
        return None

    _throttle_requests()
    resp = requests.get(
        sam_url,
        timeout=(5, 15),
        headers={"User-Agent": "Mozilla/5.0"},
    )
    logger.info("SAM page description status=%s url=%s", resp.status_code, sam_url)
    resp.raise_for_status()

    html = resp.text

    meta_matches = re.findall(
        r'<meta[^>]+(?:name|property)=["\'](?:description|og:description|twitter:description)["\'][^>]+content=["\']([^"\']+)',
        html,
        re.I,
    )
    for match in meta_matches:
        cleaned = _clean_extracted_text(match)
        if cleaned:
            return cleaned

    script_json_matches = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.I | re.S,
    )
    for match in script_json_matches:
        try:
            payload = json.loads(match)
        except ValueError:
            continue
        cleaned = _normalize_description_payload(payload)
        if cleaned:
            return cleaned

    for pattern in (
        r'"description"\s*:\s*"(.{40,4000}?)"',
        r'>\s*Description\s*<(.{40,4000}?)</',
    ):
        match = re.search(pattern, html, re.I | re.S)
        if not match:
            continue
        cleaned = _clean_extracted_text(match.group(1))
        if cleaned:
            return cleaned

    body = re.sub(r"<script\b.*?</script>", " ", html, flags=re.I | re.S)
    body = re.sub(r"<style\b.*?</style>", " ", body, flags=re.I | re.S)
    cleaned_body = _clean_extracted_text(body)
    if cleaned_body and "description" in cleaned_body.lower():
        return cleaned_body

    return None


def resolve_notice_description(description_url: str | None, sam_url: str | None) -> str | None:
    api_rate_limit_error: SamRateLimitError | None = None

    if _is_url_like(description_url):
        try:
            resolved = fetch_notice_description(description_url)
        except SamRateLimitError as exc:
            api_rate_limit_error = exc
        except requests.RequestException as exc:
            logger.warning("SAM notice description request failed url=%s error=%s", description_url, repr(exc))
        except Exception as exc:
            logger.warning("SAM notice description resolution failed url=%s error=%s", description_url, repr(exc))
        else:
            if resolved:
                return resolved

    if _is_url_like(sam_url):
        try:
            resolved = fetch_sam_page_description(sam_url)
        except requests.RequestException as exc:
            logger.warning("SAM page description request failed url=%s error=%s", sam_url, repr(exc))
        except Exception as exc:
            logger.warning("SAM page description resolution failed url=%s error=%s", sam_url, repr(exc))
        else:
            if resolved:
                return resolved

    if api_rate_limit_error is not None:
        raise api_rate_limit_error

    return None

def search_opportunities(
    *,
    naics: str,
    posted_from: dt.date,
    posted_to: dt.date,
    response_deadline_from: dt.date | None = None,
    response_deadline_to: dt.date | None = None,
    organization_name: str | None = None,
    procurement_types: list[str] | None = None,
    limit: int = 100,
    offset: int = 0,
    max_retries: int = 3,
    allow_rate_limit_wait: bool = True,
) -> Dict[str, Any]:
    api_key = SAM_API_KEY
    if not api_key:
        raise RuntimeError("SAM_API_KEY is not set")

    params = {
        "api_key": api_key,
        "postedFrom": _mmddyyyy(posted_from),
        "postedTo": _mmddyyyy(posted_to),
        "ncode": naics,
        "limit": limit,
        "offset": offset,
    }
    if response_deadline_from is not None:
        params["rdlfrom"] = _mmddyyyy(response_deadline_from)
    if response_deadline_to is not None:
        params["rdlto"] = _mmddyyyy(response_deadline_to)
    if organization_name:
        params["organizationName"] = organization_name
    if procurement_types:
        params["ptype"] = procurement_types

    backoff = 1.0
    last_exc = None

    for attempt in range(max_retries):
        try:
            _throttle_requests()
            r = requests.get(SAM_BASE, params=params, timeout=30)
            logger.info(
                "SAM request naics=%s offset=%s attempt=%s status=%s",
                naics,
                offset,
                attempt + 1,
                r.status_code,
            )

            if r.status_code == 429:
                retry_after = _extract_retry_after_value(r)
                sleep_s = _extract_retry_after_seconds(r)
                if sleep_s is None:
                    sleep_s = backoff

                logger.warning(
                    "SAM rate limited naics=%s offset=%s attempt=%s retry_after=%s sleep=%s",
                    naics,
                    offset,
                    attempt + 1,
                    retry_after,
                    sleep_s,
                )

                if not allow_rate_limit_wait:
                    raise SamRateLimitError(
                        _rate_limit_message(r, "SAM.gov is rate limiting requests. Try again later."),
                        retry_after_seconds=sleep_s,
                        retry_after=retry_after,
                    )

                if sleep_s > MAX_RATE_LIMIT_WAIT_SECONDS:
                    raise SamRateLimitError(
                        f"SAM rate limited requests for about {int(round(sleep_s))} seconds; try again later.",
                        retry_after_seconds=sleep_s,
                        retry_after=retry_after,
                    )

                if attempt == max_retries - 1:
                    raise SamRateLimitError(
                        f"SAM rate limited requests and asked us to wait about {int(round(sleep_s))} seconds.",
                        retry_after_seconds=sleep_s,
                        retry_after=retry_after,
                    )

                time.sleep(sleep_s)
                backoff = min(max(backoff * 2, sleep_s), MAX_RATE_LIMIT_WAIT_SECONDS)
                continue

            if r.status_code in TRANSIENT_SAM_STATUS_CODES:
                logger.warning(
                    "SAM transient failure naics=%s offset=%s attempt=%s status=%s body=%s",
                    naics,
                    offset,
                    attempt + 1,
                    r.status_code,
                    r.text[:400],
                )
                last_exc = SamTemporaryUnavailableError(
                    "SAM.gov is temporarily unavailable. Try again later.",
                    status_code=r.status_code,
                )
                if attempt == max_retries - 1:
                    break
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_RATE_LIMIT_WAIT_SECONDS)
                continue

            r.raise_for_status()
            payload = r.json()
            if _looks_like_sam_runtime_error(payload):
                message = _extract_sam_error_text(payload) or "SAM.gov returned a runtime error."
                logger.warning(
                    "SAM runtime error payload naics=%s offset=%s attempt=%s message=%s",
                    naics,
                    offset,
                    attempt + 1,
                    message,
                )
                last_exc = SamTemporaryUnavailableError(
                    "SAM.gov is temporarily unavailable. Try again later."
                )
                if attempt == max_retries - 1:
                    break
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_RATE_LIMIT_WAIT_SECONDS)
                continue
            return payload

        except SamRateLimitError:
            raise
        except SamTemporaryUnavailableError:
            raise
        except requests.RequestException as e:
            logger.warning(
                "SAM request exception naics=%s offset=%s attempt=%s error=%s",
                naics,
                offset,
                attempt + 1,
                repr(e),
            )
            last_exc = e
            if attempt == max_retries - 1:
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_RATE_LIMIT_WAIT_SECONDS)

    # If we exhausted retries, raise the last exception
    if isinstance(last_exc, SamTemporaryUnavailableError):
        raise last_exc
    if isinstance(last_exc, requests.RequestException) and getattr(last_exc, "response", None) is None:
        raise SamTemporaryUnavailableError("SAM.gov is temporarily unavailable. Try again later.")
    if last_exc is not None and getattr(last_exc, "response", None) is not None:
        resp = last_exc.response
        raise RuntimeError(
            f"SAM request failed after retries: status={resp.status_code} url={resp.url} body={resp.text[:800]}"
        )
    raise RuntimeError(f"SAM request failed after retries: {repr(last_exc)}")


def fetch_notice_description(description_url: str) -> str | None:
    if not _is_url_like(description_url):
        return None

    api_key = SAM_API_KEY
    if not api_key:
        raise RuntimeError("SAM_API_KEY is not set")

    request_url = _with_api_key(description_url, api_key)
    _throttle_requests()
    resp = requests.get(request_url, timeout=30)
    logger.info("SAM notice description status=%s url=%s", resp.status_code, description_url)

    if resp.status_code == 429:
        retry_after = _extract_retry_after_seconds(resp)
        retry_after_value = _extract_retry_after_value(resp)
        raise SamRateLimitError(
            _rate_limit_message(resp, "SAM rate limited notice description fetch."),
            retry_after_seconds=retry_after,
            retry_after=retry_after_value,
        )

    resp.raise_for_status()

    content_type = (resp.headers.get("Content-Type") or "").lower()
    if "json" in content_type:
        try:
            normalized = _normalize_description_payload(resp.json())
        except ValueError:
            normalized = None
        if normalized:
            return normalized

    normalized = _normalize_description_payload(resp.text)
    return normalized
