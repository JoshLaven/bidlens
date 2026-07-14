import logging
from typing import Any

import requests
from requests import HTTPError

from .config import GRANTS_GOV_SEARCH_URL


logger = logging.getLogger(__name__)
GRANTS_GOV_DETAIL_URL = "https://api.grants.gov/v1/api/fetchOpportunity"
DEFAULT_GRANTS_POSTED_DAYS_BACK = 7
DEFAULT_GRANTS_ROWS = 25


class GrantsGovApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, response_text: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


def _response_excerpt(response: requests.Response) -> str:
    text = (response.text or "").strip()
    return text[:1000]


def _post_search(payload: dict[str, Any]) -> requests.Response:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    response = requests.post(GRANTS_GOV_SEARCH_URL, json=payload, headers=headers, timeout=30)
    logger.info("Grants.gov search status=%s url=%s", response.status_code, GRANTS_GOV_SEARCH_URL)
    return response


def _post_json(url: str, payload: dict[str, Any]) -> requests.Response:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    response = requests.post(url, json=payload, headers=headers, timeout=30)
    logger.info("Grants.gov POST status=%s url=%s", response.status_code, url)
    return response


def _json_or_error(response: requests.Response) -> dict[str, Any]:
    try:
        response.raise_for_status()
    except HTTPError as exc:
        raise GrantsGovApiError(
            f"Grants.gov API returned HTTP {response.status_code}: {_response_excerpt(response) or exc}",
            status_code=response.status_code,
            response_text=_response_excerpt(response),
        ) from exc
    try:
        return response.json()
    except ValueError as exc:
        raise GrantsGovApiError(
            f"Grants.gov API returned invalid JSON: {_response_excerpt(response) or exc}",
            status_code=response.status_code,
            response_text=_response_excerpt(response),
        ) from exc


def search_recent_opportunities(
    *,
    days_back: int = DEFAULT_GRANTS_POSTED_DAYS_BACK,
    rows: int = DEFAULT_GRANTS_ROWS,
    start_record_num: int = 0,
) -> dict[str, Any]:
    # Grants.gov's public Search2 endpoint does not require authentication.
    # Keep GRANTS_GOV_API_KEY configured for future endpoints, but do not send
    # it here because the search API rejects the old API-Gateway-style path.
    # Search2 interprets dateRange as a Posted Date window in days. Its live
    # production endpoint accepts "1" even though the returned facet options
    # begin at seven days.
    date_range = str(days_back) if days_back in {1, 3, 7, 14, 21, 30, 60, 90} else ""
    payload = {
        "startRecordNum": max(0, int(start_record_num)),
        "rows": rows,
        "keyword": "",
        "oppNum": "",
        "eligibilities": "",
        "agencies": "",
        "fundingCategories": "",
        "fundingInstruments": "",
        "oppStatuses": "forecasted|posted",
        "dateRange": date_range,
    }
    response = _post_search(payload)
    logger.info(
        "Grants.gov search rows=%s days_back=%s start_record_num=%s",
        rows,
        days_back,
        start_record_num,
    )
    return _json_or_error(response)


def fetch_opportunity_detail(opportunity_id: str) -> dict[str, Any]:
    response = _post_json(GRANTS_GOV_DETAIL_URL, {"opportunityId": str(opportunity_id)})
    return _json_or_error(response)
