# src/bidlens/sam_client.py
import os
import datetime as dt
import time
import requests
from typing import Any, Dict

SAM_BASE = "https://api.sam.gov/opportunities/v2/search"

def _mmddyyyy(d: dt.date) -> str:
    return d.strftime("%m/%d/%Y")

def search_opportunities(
    *,
    naics: str,
    posted_from: dt.date,
    posted_to: dt.date,
    limit: int = 100,
    offset: int = 0,
    max_retries: int = 5,
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
            r = requests.get(SAM_BASE, params=params, timeout=30)

            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    try:
                        sleep_s = float(retry_after)
                    except ValueError:
                        sleep_s = backoff
                else:
                    sleep_s = backoff

                time.sleep(sleep_s)
                backoff = min(backoff * 2, 30)  # cap
                continue

            r.raise_for_status()
            return r.json()

        except requests.RequestException as e:
            print("[SAM_CLIENT EXC]", type(e).__name__, str(e))
            last_exc = e
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)

    # If we exhausted retries, raise the last exception
    if last_exc is not None and getattr(last_exc, "response", None) is not None:
        resp = last_exc.response
        raise RuntimeError(
            f"SAM request failed after retries: status={resp.status_code} url={resp.url} body={resp.text[:800]}"
        )
    raise RuntimeError(f"SAM request failed after retries: {repr(last_exc)}")
