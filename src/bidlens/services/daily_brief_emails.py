from __future__ import annotations

import datetime as dt
from html import escape
import re
from typing import Any
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from ..config import BIDLENS_APP_BASE_URL
from ..models import DailySnapshot, Workspace
from .email_delivery import EmailMessage


MAX_EMAIL_OPPORTUNITIES = 10
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_recipient_email(value: str | None) -> bool:
    return bool(value and EMAIL_RE.match(value.strip()))


def _base_url(app_base_url: str | None = None) -> str:
    return (app_base_url or BIDLENS_APP_BASE_URL or "http://localhost:8000").rstrip("/")


def _feed_url(*, organization_id: int, app_base_url: str | None = None) -> str:
    return f"{_base_url(app_base_url)}/?{urlencode({'org_id': organization_id})}"


def _opportunity_url(opportunity_id: int | None, *, organization_id: int, app_base_url: str | None = None) -> str:
    if opportunity_id:
        return f"{_base_url(app_base_url)}/opportunity/{opportunity_id}?{urlencode({'org_id': organization_id})}"
    return _feed_url(organization_id=organization_id, app_base_url=app_base_url)


def _display_date(value: str | dt.date | None) -> str:
    if isinstance(value, dt.date):
        return f"{value:%b} {value.day}, {value:%Y}"
    if isinstance(value, str) and value:
        try:
            parsed = dt.date.fromisoformat(value[:10])
            return f"{parsed:%b} {parsed.day}, {parsed:%Y}"
        except Exception:
            return value
    return "Not specified"


def _first_name(name: str | None, email: str) -> str:
    label = (name or "").strip() or email.split("@", 1)[0]
    return label.split()[0] if label else "there"


def _snapshot_for_user(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    snapshot_date: dt.date,
) -> DailySnapshot | None:
    return (
        db.query(DailySnapshot)
        .filter(
            DailySnapshot.workspace_id == workspace_id,
            DailySnapshot.user_id == user_id,
            DailySnapshot.snapshot_date == snapshot_date,
        )
        .first()
    )


def _feed_items_from_snapshot(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("new_feed_opportunities")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    return []


def _refresh_feed_items_if_missing(
    db: Session,
    *,
    snapshot: DailySnapshot,
) -> list[dict[str, Any]]:
    payload = dict(snapshot.snapshot_json or {})
    items = _feed_items_from_snapshot(payload)
    if items:
        return items
    # Snapshots generated before Daily Brief Email V1 may not include the
    # opportunity-level Feed list. For that narrow compatibility case, return an
    # empty list instead of rebuilding broad Daily Brief logic or widening email
    # content beyond Feed opportunities.
    return []


def build_daily_brief_email_message(
    db: Session,
    *,
    workspace: Workspace,
    user_id: int,
    user_name: str | None,
    user_email: str,
    snapshot_date: dt.date,
    app_base_url: str | None = None,
) -> tuple[EmailMessage | None, int, str | None]:
    snapshot = _snapshot_for_user(
        db,
        workspace_id=workspace.id,
        user_id=user_id,
        snapshot_date=snapshot_date,
    )
    if not snapshot:
        return None, 0, "No current-day Daily Snapshot exists."

    payload = snapshot.snapshot_json or {}
    feed_items = _refresh_feed_items_if_missing(db, snapshot=snapshot)
    feed_count = int((payload.get("summary") or {}).get("new_feed_count") or len(feed_items))
    visible_items = feed_items[:MAX_EMAIL_OPPORTUNITIES]
    first_name = _first_name(user_name, user_email)
    subject = f"{first_name}'s BidLens Daily Brief"
    feed_url = _feed_url(organization_id=workspace.organization_id, app_base_url=app_base_url)
    snapshot_label = f"{snapshot_date:%b} {snapshot_date.day}, {snapshot_date:%Y}"

    if feed_count:
        lead = f"{feed_count} new Feed opportunit{'y' if feed_count == 1 else 'ies'} are ready for review."
    else:
        lead = "No new Feed opportunities were added yesterday."

    html_items = []
    text_items = []
    for item in visible_items:
        title = str(item.get("title") or "Untitled opportunity")
        agency = item.get("agency") or item.get("source_label") or item.get("source") or ""
        due = _display_date(item.get("response_deadline")) if item.get("response_deadline") else ""
        subtitle = " · ".join(str(part) for part in (agency, f"Due {due}" if due else None) if part)
        destination = _opportunity_url(
            item.get("id"),
            organization_id=workspace.organization_id,
            app_base_url=app_base_url,
        )
        html_items.append(
            "<li>"
            f"<a href=\"{escape(destination)}\">{escape(title)}</a>"
            + (f"<br><span>{escape(subtitle)}</span>" if subtitle else "")
            + "</li>"
        )
        text_items.append(f"- {title}" + (f" ({subtitle})" if subtitle else ""))

    if not html_items:
        html_list = "<p>You are all caught up. Open BidLens any time to review your Feed.</p>"
        text_list = "You are all caught up. Open BidLens any time to review your Feed."
    else:
        html_list = "<ul>" + "".join(html_items) + "</ul>"
        text_list = "\n".join(text_items)

    html_body = f"""<!doctype html>
<html>
  <body>
    <h1>{escape(first_name)}'s Daily Brief</h1>
    <p>{escape(snapshot_label)}</p>
    <p>{escape(lead)}</p>
    {html_list}
    <p><a href="{escape(feed_url)}">Open your BidLens Feed</a></p>
  </body>
</html>
"""
    text_body = "\n\n".join([
        f"{first_name}'s Daily Brief",
        snapshot_label,
        lead,
        text_list,
        f"Open your BidLens Feed: {feed_url}",
    ])
    return EmailMessage(
        to_email=user_email.strip(),
        subject=subject,
        html_body=html_body,
        text_body=text_body,
    ), feed_count, None
