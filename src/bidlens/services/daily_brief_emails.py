from __future__ import annotations

import datetime as dt
from html import escape
import re
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from ..config import BIDLENS_APP_BASE_URL
from ..models import DailySnapshot, Opportunity, OpportunityPursuitLaneMatch, PursuitLane, Workspace
from .email_delivery import EmailMessage
from .feed_queries import feed_awaiting_review_query


MAX_EMAIL_SHORTLIST_ACTIVITY = 5
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


def _current_feed_summary(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
) -> tuple[int, list[dict[str, Any]]]:
    feed_query = feed_awaiting_review_query(
        db,
        organization_id=organization_id,
        user_id=user_id,
        include_watched=False,
    )
    feed_count = int(feed_query.with_entities(func.count(Opportunity.id)).scalar() or 0)
    if not feed_count:
        return 0, []

    feed_rows = (
        feed_query
        .with_entities(Opportunity.id)
        .order_by(Opportunity.response_deadline.asc(), Opportunity.id.asc())
        .all()
    )
    feed_ids = [int(row.id) for row in feed_rows]
    lane_rows = (
        db.query(OpportunityPursuitLaneMatch.opportunity_id, PursuitLane.id, PursuitLane.name)
        .join(
            PursuitLane,
            and_(
                PursuitLane.id == OpportunityPursuitLaneMatch.pursuit_lane_id,
                PursuitLane.organization_id == organization_id,
                PursuitLane.is_active.is_(True),
            ),
        )
        .filter(
            OpportunityPursuitLaneMatch.organization_id == organization_id,
            OpportunityPursuitLaneMatch.opportunity_id.in_(feed_ids),
        )
        .order_by(
            OpportunityPursuitLaneMatch.opportunity_id.asc(),
            PursuitLane.name.asc(),
            PursuitLane.id.asc(),
        )
        .all()
    )
    primary_lane_by_opp: dict[int, tuple[int, str]] = {}
    for opportunity_id, lane_id, lane_name in lane_rows:
        primary_lane_by_opp.setdefault(int(opportunity_id), (int(lane_id), str(lane_name)))

    counts: dict[tuple[int | None, str], int] = {}
    for opportunity_id in feed_ids:
        bucket = primary_lane_by_opp.get(opportunity_id) or (None, "Unassigned")
        counts[bucket] = counts.get(bucket, 0) + 1

    lanes = [
        {"id": lane_id, "name": lane_name, "count": count}
        for (lane_id, lane_name), count in counts.items()
        if count > 0
    ]
    lanes.sort(key=lambda item: (-int(item["count"]), str(item["name"])))
    return feed_count, lanes


def _shortlist_activity_from_snapshot(payload: dict[str, Any]) -> list[dict[str, Any]]:
    activity: list[dict[str, Any]] = []
    for key in ("team_signals", "shortlist_updates"):
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                activity.append(item)
    return activity[:MAX_EMAIL_SHORTLIST_ACTIVITY]


def _activity_title(item: dict[str, Any]) -> str:
    if item.get("title"):
        return str(item["title"])
    opportunity = item.get("opportunity")
    if isinstance(opportunity, dict) and opportunity.get("title"):
        return str(opportunity["title"])
    return "Opportunity activity"


def _activity_subtitle(item: dict[str, Any]) -> str:
    if item.get("subtitle"):
        return str(item["subtitle"])
    if item.get("action"):
        return str(item["action"])
    changed_fields = item.get("changed_fields")
    if isinstance(changed_fields, list) and changed_fields:
        labels = [str(field).replace("_", " ").title() for field in changed_fields[:2]]
        return f"{', '.join(labels)} changed"
    return "Needs attention"


def _activity_destination(item: dict[str, Any], *, organization_id: int, app_base_url: str | None = None) -> str:
    opportunity = item.get("opportunity")
    opportunity_id = item.get("id")
    if isinstance(opportunity, dict):
        opportunity_id = opportunity.get("id") or opportunity_id
    try:
        parsed_id = int(opportunity_id) if opportunity_id else None
    except (TypeError, ValueError):
        parsed_id = None
    return _opportunity_url(
        parsed_id,
        organization_id=organization_id,
        app_base_url=app_base_url,
    )


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
    feed_count, lane_breakdown = _current_feed_summary(
        db,
        organization_id=workspace.organization_id,
        user_id=user_id,
    )
    shortlist_activity = _shortlist_activity_from_snapshot(payload)
    first_name = _first_name(user_name, user_email)
    subject = f"{first_name}'s BidLens Daily Brief"
    feed_url = _feed_url(organization_id=workspace.organization_id, app_base_url=app_base_url)
    shortlist_url = f"{_base_url(app_base_url)}/my-shortlist?{urlencode({'org_id': workspace.organization_id})}"
    snapshot_label = f"{snapshot_date:%b} {snapshot_date.day}, {snapshot_date:%Y}"

    if feed_count:
        lead = f"{feed_count} opportunit{'y is' if feed_count == 1 else 'ies are'} awaiting review."
    else:
        lead = "No opportunities are awaiting review."

    lane_html_items = [
        f"<li>{escape(str(item['name']))} — {int(item['count'])}</li>"
        for item in lane_breakdown
    ]
    lane_text_items = [
        f"- {item['name']} — {int(item['count'])}"
        for item in lane_breakdown
    ]

    if lane_html_items:
        feed_detail_html = "<ul>" + "".join(lane_html_items) + "</ul>"
        feed_detail_text = "\n".join(lane_text_items)
    else:
        feed_detail_html = "<p>You are all caught up. Open BidLens any time to review your Feed.</p>"
        feed_detail_text = "You are all caught up. Open BidLens any time to review your Feed."

    shortlist_html = ""
    shortlist_text = ""
    if shortlist_activity:
        shortlist_count = len(shortlist_activity)
        shortlist_lead = f"{shortlist_count} opportunit{'y needs' if shortlist_count == 1 else 'ies need'} your attention."
        shortlist_items_html = []
        shortlist_items_text = []
        for item in shortlist_activity:
            title = _activity_title(item)
            subtitle = _activity_subtitle(item)
            destination = _activity_destination(
                item,
                organization_id=workspace.organization_id,
                app_base_url=app_base_url,
            )
            shortlist_items_html.append(
                "<li>"
                f"<a href=\"{escape(destination)}\">{escape(title)}</a>"
                f"<br><span>{escape(subtitle)}</span>"
                "</li>"
            )
            shortlist_items_text.append(f"- {title} ({subtitle})")
        shortlist_html = (
            "<h2>Shortlist</h2>"
            f"<p>{escape(shortlist_lead)}</p>"
            "<ul>" + "".join(shortlist_items_html) + "</ul>"
            f"<p><a href=\"{escape(shortlist_url)}\">View Shortlist</a></p>"
        )
        shortlist_text = "\n\n".join([
            "Shortlist",
            shortlist_lead,
            "\n".join(shortlist_items_text),
            f"View Shortlist: {shortlist_url}",
        ])

    html_body = f"""<!doctype html>
<html>
  <body>
    <h1>{escape(first_name)}'s Daily Brief</h1>
    <p>{escape(snapshot_label)}</p>
    <h2>Feed</h2>
    <p>{escape(lead)}</p>
    {feed_detail_html}
    <p><a href="{escape(feed_url)}">Review Feed</a></p>
    {shortlist_html}
  </body>
</html>
"""
    text_parts = [
        f"{first_name}'s Daily Brief",
        snapshot_label,
        "Feed",
        lead,
        feed_detail_text,
        f"Review Feed: {feed_url}",
    ]
    if shortlist_text:
        text_parts.append(shortlist_text)
    text_body = "\n\n".join(text_parts)
    return EmailMessage(
        to_email=user_email.strip(),
        subject=subject,
        html_body=html_body,
        text_body=text_body,
    ), feed_count, None
