import os
import datetime as dt
import time
import threading
import logging
from email.utils import parsedate_to_datetime
import requests
from typing import Any, Dict

SAM_BASE = "https://api.sam.gov/opportunities/v2/search"
logger = logging.getLogger(__name__)
_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0
MIN_REQUEST_INTERVAL_SECONDS = 1.0
MAX_RATE_LIMIT_WAIT_SECONDS = 30.0


class SamRateLimitError(RuntimeError):
    def __init__(self, message: str, *, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds

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

def search_opportunities(
    *,
    naics: str,
    posted_from: dt.date,
    posted_to: dt.date,
    limit: int = 100,
    offset: int = 0,
    max_retries: int = 2,
) -> Dict[str, Any]:
    api_key = os.getenv("SAM_API_KEY")
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
                retry_after = r.headers.get("Retry-After")
                sleep_s = _parse_retry_after(retry_after)
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

                if sleep_s > MAX_RATE_LIMIT_WAIT_SECONDS:
                    raise SamRateLimitError(
                        f"SAM rate limited requests for about {int(round(sleep_s))} seconds; try again later.",
                        retry_after_seconds=sleep_s,
                    )

                if attempt == max_retries - 1:
                    raise SamRateLimitError(
                        f"SAM rate limited requests and asked us to wait about {int(round(sleep_s))} seconds.",
                        retry_after_seconds=sleep_s,
                    )

                time.sleep(sleep_s)
                backoff = min(max(backoff * 2, sleep_s), MAX_RATE_LIMIT_WAIT_SECONDS)
                continue

            r.raise_for_status()
            return r.json()

        except SamRateLimitError:
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
    if last_exc is not None and getattr(last_exc, "response", None) is not None:
        resp = last_exc.response
        raise RuntimeError(
            f"SAM request failed after retries: status={resp.status_code} url={resp.url} body={resp.text[:800]}"
        )
    raise RuntimeError(f"SAM request failed after retries: {repr(last_exc)}")
