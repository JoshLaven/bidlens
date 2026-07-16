from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import (
    CompanyProfile,
    DailySnapshot,
    Event,
    IngestionRun,
    Opportunity,
    OpportunityUpdateEvent,
    Organization,
    OrganizationMembership,
    OrgProfile,
    PursuitLane,
    SamSourceConfig,
    Workspace,
)
from .feed_queries import feed_awaiting_review_query
from .salesforce import SalesforceService


SUCCESSFUL_RUN_STATUSES = ("success", "completed")
ISSUE_RUN_STATUSES = ("failed", "error", "partial_success")
SOURCE_LABELS = {
    "sam": "SAM.gov",
    "sam.gov": "SAM.gov",
    "grants_gov": "Grants.gov",
    "grants.gov": "Grants.gov",
    "govwin_api": "GovWin",
    "govwin_export": "GovWin",
}
SETUP_EVENT_TITLES = {
    "organization_created": "Organization created",
    "company_profile_configured": "Organization identity completed",
    "opportunity_sources_connected": "Opportunity sources connected",
    "first_successful_import": "First successful import",
    "users_invited": "Users invited",
    "salesforce_connected": "CRM connected",
    "feed_rules_configured": "Feed rules configured",
    "pursuit_lanes_configured": "Pursuit lanes configured",
    "workspace_went_live": "Workspace went live",
}


def _workspace_url(path: str, organization_id: int, *, fragment: str | None = None) -> str:
    url = f"{path}?{urlencode({'org_id': organization_id})}"
    return f"{url}#{fragment}" if fragment else url


def _source_label(source: str | None) -> str:
    normalized = str(source or "").strip().lower()
    return SOURCE_LABELS.get(normalized, str(source or "Unknown source"))


def _latest_successful_import(db: Session, organization_id: int) -> IngestionRun | None:
    return (
        db.query(IngestionRun)
        .filter(
            IngestionRun.organization_id == organization_id,
            IngestionRun.status.in_(SUCCESSFUL_RUN_STATUSES),
            IngestionRun.error_count == 0,
        )
        .order_by(IngestionRun.finished_at.desc(), IngestionRun.started_at.desc(), IngestionRun.id.desc())
        .first()
    )


def _configured_source_labels(
    db: Session,
    *,
    organization_id: int,
    profile: OrgProfile | None,
) -> list[str]:
    labels: set[str] = set()

    if (
        db.query(SamSourceConfig.id)
        .filter(SamSourceConfig.organization_id == organization_id)
        .first()
    ):
        labels.add("SAM.gov")

    if profile and profile.govwin_credentials_encrypted:
        labels.add("GovWin")

    grants_enabled = (
        db.query(Event.id)
        .filter(
            Event.org_id == organization_id,
            Event.event_type == "opportunity_source_enabled",
            Event.payload["source"].as_string() == "grants.gov",
        )
        .first()
    )
    if grants_enabled:
        labels.add("Grants.gov")

    # A successful import is durable evidence that a manual/public source is in
    # use even when that connector has no organization-level configuration row.
    imported_sources = (
        db.query(IngestionRun.source)
        .filter(
            IngestionRun.organization_id == organization_id,
            IngestionRun.status.in_(SUCCESSFUL_RUN_STATUSES),
            IngestionRun.error_count == 0,
        )
        .distinct()
        .all()
    )
    labels.update(_source_label(source) for (source,) in imported_sources if source)
    return sorted(labels)


def _connector_attention(
    db: Session,
    *,
    organization_id: int,
) -> list[dict[str, Any]]:
    runs = (
        db.query(IngestionRun)
        .filter(IngestionRun.organization_id == organization_id)
        .order_by(IngestionRun.started_at.desc(), IngestionRun.id.desc())
        .all()
    )
    latest_by_source: dict[str, IngestionRun] = {}
    for run in runs:
        label = _source_label(run.source)
        latest_by_source.setdefault(label, run)

    items: list[dict[str, Any]] = []
    for source_label, run in latest_by_source.items():
        status = str(run.status or "").strip().lower()
        if status not in ISSUE_RUN_STATUSES and not (run.error_count or 0):
            continue
        items.append(
            {
                "key": f"connector-{source_label.lower().replace('.', '').replace(' ', '-')}",
                "title": "Opportunity source import needs attention",
                "description": run.notes or "The most recent import did not complete cleanly.",
                "occurred_at": run.finished_at or run.started_at,
                "cta_label": "Open Connector Operations",
                "cta_url": _workspace_url("/integrations", organization_id),
            }
        )
    return items


def _recommendation(
    *,
    key: str,
    title: str,
    label: str,
    description: str,
    cta_label: str,
    cta_url: str,
    priority: int,
) -> dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "label": label,
        "status": label,
        "description": description,
        "destination": cta_label,
        "cta_label": cta_label,
        "cta_url": cta_url,
        "priority": priority,
        "order": priority,
    }


def _completed_item(
    *,
    key: str,
    title: str,
    completed_at: datetime | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "description": description,
        "completed_at": completed_at,
    }


def _setup_history_from_events(db: Session, organization_id: int) -> list[dict[str, Any]]:
    events = (
        db.query(Event)
        .filter(
            Event.org_id == organization_id,
            Event.event_type.in_(tuple(SETUP_EVENT_TITLES)),
        )
        .order_by(Event.ts.desc(), Event.id.desc())
        .limit(12)
        .all()
    )
    return [
        {
            "key": event.event_type,
            "title": SETUP_EVENT_TITLES.get(event.event_type, event.event_type.replace("_", " ").title()),
            "occurred_at": event.ts,
            "payload": event.payload or {},
        }
        for event in events
    ]


def _has_setup_event(db: Session, organization_id: int, event_type: str) -> bool:
    return bool(
        db.query(Event.id)
        .filter(Event.org_id == organization_id, Event.event_type == event_type)
        .first()
    )


def _stored_daily_snapshot_context(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    today: datetime,
) -> dict[str, Any] | None:
    workspace = (
        db.query(Workspace)
        .filter(Workspace.organization_id == organization_id)
        .first()
    )
    if not workspace:
        return None

    snapshot = (
        db.query(DailySnapshot)
        .filter(
            DailySnapshot.workspace_id == workspace.id,
            DailySnapshot.user_id == user_id,
            DailySnapshot.snapshot_date == today.date(),
        )
        .first()
    )
    if not snapshot:
        return None

    payload = snapshot.snapshot_json or {}
    return {
        "id": snapshot.id,
        "workspace_id": snapshot.workspace_id,
        "user_id": snapshot.user_id,
        "snapshot_date": snapshot.snapshot_date,
        "created_at": snapshot.created_at,
        "status": snapshot.status,
        "snapshot_json": payload,
        "sections": {
            "my_shortlist": payload.get("my_shortlist") or [],
            "team_signals": payload.get("team_signals") or [],
            "my_lanes": payload.get("my_lanes") or [],
            "new_opportunities": payload.get("new_opportunities") or [],
            "updated_opportunities": payload.get("updated_opportunities") or [],
            "upcoming_deadlines": payload.get("upcoming_deadlines") or [],
            "interested_activity": payload.get("interested_activity") or [],
            "shortlist_changes": payload.get("shortlist_changes") or [],
            "connector_issues": payload.get("connector_issues") or [],
        },
    }


def _daily_brief_item(raw_item: Any) -> dict[str, str]:
    if not isinstance(raw_item, dict):
        return {"title": str(raw_item), "subtitle": "", "destination_url": ""}
    opportunity = raw_item.get("opportunity")
    if isinstance(opportunity, dict):
        title = str(raw_item.get("title") or opportunity.get("title") or "Untitled opportunity")
        destination_url = str(raw_item.get("destination_url") or f"/opportunity/{opportunity.get('id')}")
    else:
        title = str(raw_item.get("title") or "Untitled update")
        destination_url = str(raw_item.get("destination_url") or "")
    return {
        "title": title,
        "subtitle": str(raw_item.get("subtitle") or ""),
        "destination_url": destination_url,
    }


def _changed_field_label(changed_fields: Any) -> str:
    if not isinstance(changed_fields, dict) or not changed_fields:
        return "Updated yesterday"
    field_names = [
        str(field).replace("_", " ").title()
        for field in changed_fields.keys()
    ]
    return "Updated: " + ", ".join(field_names[:3])


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else (plural or f"{singular}s")


def _daily_brief_section_item(section_key: str, raw_item: Any) -> dict[str, str]:
    if section_key in {"my_shortlist", "team_signals", "my_lanes"}:
        return _daily_brief_item(raw_item)
    if not isinstance(raw_item, dict):
        return _daily_brief_item(raw_item)

    opportunity = raw_item.get("opportunity") if isinstance(raw_item.get("opportunity"), dict) else raw_item
    title = str(opportunity.get("title") or raw_item.get("title") or "Untitled opportunity")
    destination_url = str(
        raw_item.get("destination_url")
        or (f"/opportunity/{opportunity.get('id')}" if opportunity.get("id") else "")
    )

    if section_key == "new_opportunities":
        agency = opportunity.get("agency")
        due = opportunity.get("response_deadline")
        subtitle = " · ".join([str(part) for part in (agency, f"Due {due}" if due else None) if part])
    elif section_key == "updated_opportunities":
        subtitle = _changed_field_label(raw_item.get("changed_fields"))
    elif section_key == "upcoming_deadlines":
        days = raw_item.get("days_until_deadline")
        due = opportunity.get("response_deadline")
        if days == 0:
            subtitle = "Due today"
        elif days == 1:
            subtitle = "Due tomorrow"
        elif days is not None:
            subtitle = f"Due in {days} days"
        else:
            subtitle = f"Due {due}" if due else "Upcoming deadline"
    elif section_key == "interested_activity":
        user = raw_item.get("user") or {}
        actor = user.get("name") or user.get("email") or "A teammate"
        action = "removed interest" if raw_item.get("toggled_off") else "showed interest"
        subtitle = f"{actor} {action}"
    elif section_key == "shortlist_changes":
        user = raw_item.get("user") or {}
        actor = user.get("name") or user.get("email") or "A teammate"
        from_state = raw_item.get("from") or "None"
        to_state = raw_item.get("to") or "None"
        subtitle = f"{actor}: {from_state} to {to_state}"
    elif section_key == "connector_issues":
        title = str(raw_item.get("source_label") or raw_item.get("source") or "Connector issue")
        status = raw_item.get("status")
        notes = raw_item.get("notes")
        subtitle = " · ".join([str(part) for part in (status, notes) if part])
        destination_url = "/opportunity-discovery"
    else:
        subtitle = str(raw_item.get("subtitle") or "")

    return {
        "title": title,
        "subtitle": subtitle,
        "destination_url": destination_url,
    }


def _feed_awaiting_review_count(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
) -> int:
    return (
        feed_awaiting_review_query(
            db,
            organization_id=organization_id,
            user_id=user_id,
            include_watched=False,
        )
        .with_entities(func.count(Opportunity.id))
        .scalar()
        or 0
    )


def _daily_brief_points(payload: dict[str, Any]) -> list[str]:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    shortlist_update_count = int(summary.get("shortlist_update_count") or len(payload.get("shortlist_updates") or []))
    team_signal_count = int(summary.get("team_signal_count") or len(payload.get("team_signals") or []))
    shortlist_deadline_count = int(summary.get("shortlist_deadline_count") or len(payload.get("shortlist_deadlines") or []))
    connector_issue_count = int(summary.get("connector_issue_count") or len(payload.get("connector_issues") or []))

    points = []
    if shortlist_update_count:
        points.append(
            f"{shortlist_update_count} {_plural(shortlist_update_count, 'opportunity', 'opportunities')} "
            f"on your Shortlist changed."
        )
    if team_signal_count:
        points.append(
            f"{team_signal_count} {_plural(team_signal_count, 'teammate')} joined "
            f"{_plural(team_signal_count, 'an opportunity', 'opportunities')} you're tracking."
        )
    if shortlist_deadline_count:
        points.append(
            f"{shortlist_deadline_count} {_plural(shortlist_deadline_count, 'opportunity', 'opportunities')} "
            f"on your Shortlist {_plural(shortlist_deadline_count, 'is', 'are')} due within the next 7 days."
        )
    if connector_issue_count:
        points.append(
            f"{connector_issue_count} {_plural(connector_issue_count, 'source')} needs attention."
        )
    return points


def _feed_review_context(count: int) -> dict[str, Any]:
    if count:
        return {
            "count": count,
            "message": f"{count} {_plural(count, 'opportunity', 'opportunities')} awaiting review.",
            "complete": False,
            "url": "/",
            "action_label": "Review Feed",
        }
    return {
        "count": 0,
        "message": "No opportunities awaiting review.",
        "complete": True,
        "url": None,
        "action_label": None,
    }


def get_daily_brief_home_context(
    db: Session,
    organization_id: int,
    user_id: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the stored Daily Snapshot sections Home is allowed to render."""
    now = now or datetime.now(timezone.utc)
    snapshot = _stored_daily_snapshot_context(
        db,
        organization_id=organization_id,
        user_id=user_id,
        today=now,
    )
    payload = snapshot["snapshot_json"] if snapshot else {}
    feed_review = _feed_review_context(
        _feed_awaiting_review_count(
            db,
            organization_id=organization_id,
            user_id=user_id,
        )
    ) if snapshot else None
    section_defs = [
        ("shortlist_updates", "Shortlist Changes"),
        ("shortlist_deadlines", "Shortlist Deadlines"),
        ("team_signals", "Team Signals"),
        ("connector_issues", "Source Issues"),
    ]
    sections = []
    for key, title in section_defs:
        items = [
            _daily_brief_section_item(key, item)
            for item in (payload.get(key) or [])[:5]
        ]
        if not items:
            continue
        sections.append({
            "key": key,
            "title": title,
            "count": len(items),
            "items": items,
        })
    needs_shortlist_review = any(
        payload.get(key)
        for key in ("shortlist_updates", "shortlist_deadlines", "team_signals")
    )

    return {
        "snapshot": snapshot,
        "snapshot_date": snapshot["snapshot_date"] if snapshot else now.date(),
        "activity_date": payload.get("activity_date") if snapshot else None,
        "snapshot_missing": snapshot is None,
        "brief_points": _daily_brief_points(payload) if snapshot else [],
        "feed_review": feed_review,
        "sections": sections,
        "has_updates": bool(
            sections
            or (_daily_brief_points(payload) if snapshot else [])
            or (feed_review if snapshot else None)
        ),
        "actions": [
            {"label": "Review Shortlist", "url": "/my-shortlist"},
        ] if snapshot and needs_shortlist_review else [],
    }


def get_home_context(
    db: Session,
    organization_id: int,
    user_id: int,
    *,
    now: datetime | None = None,
    salesforce_connected: bool | None = None,
) -> dict[str, Any]:
    """Return organization-scoped, presentation-ready data for Home."""
    organization = (
        db.query(Organization)
        .filter(Organization.id == organization_id)
        .first()
    )
    if organization is None:
        raise ValueError(f"Organization {organization_id} was not found")

    now = now or datetime.now(timezone.utc)
    active_profile = (
        db.query(CompanyProfile)
        .filter(
            CompanyProfile.org_id == organization_id,
            CompanyProfile.archived_at.is_(None),
        )
        .order_by(CompanyProfile.updated_at.desc(), CompanyProfile.id.desc())
        .first()
    )
    org_profile = db.query(OrgProfile).filter(OrgProfile.org_id == organization_id).first()
    source_labels = _configured_source_labels(
        db,
        organization_id=organization_id,
        profile=org_profile,
    )
    opportunity_count = (
        db.query(func.count(Opportunity.id))
        .filter(Opportunity.organization_id == organization_id)
        .scalar()
        or 0
    )
    awaiting_review_count = (
        db.query(func.count(Opportunity.id))
        .filter(
            Opportunity.organization_id == organization_id,
            Opportunity.decision_state == "INBOX",
            Opportunity.qualification_status == "unreviewed",
        )
        .scalar()
        or 0
    )
    member_count = (
        db.query(func.count(OrganizationMembership.id))
        .filter(OrganizationMembership.organization_id == organization_id)
        .scalar()
        or 0
    )
    lane_count = (
        db.query(func.count(PursuitLane.id))
        .filter(PursuitLane.organization_id == organization_id)
        .scalar()
        or 0
    )
    recent_since = now - timedelta(days=7)
    recent_update_count = (
        db.query(func.count(OpportunityUpdateEvent.id))
        .filter(
            OpportunityUpdateEvent.organization_id == organization_id,
            OpportunityUpdateEvent.detected_at >= recent_since,
        )
        .scalar()
        or 0
    )
    last_successful_import = _latest_successful_import(db, organization_id)
    attention_items = _connector_attention(db, organization_id=organization_id)

    if salesforce_connected is None:
        salesforce_connected = SalesforceService(
            db=db, workspace_id=organization_id
        ).has_stored_authorization

    last_import_at = (
        (last_successful_import.finished_at or last_successful_import.started_at)
        if last_successful_import
        else None
    )
    daily_snapshot = _stored_daily_snapshot_context(
        db,
        organization_id=organization_id,
        user_id=user_id,
        today=now,
    )

    recommendations: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = [
        _completed_item(
            key="organization-created",
            title="Organization created",
            completed_at=organization.created_at,
        )
    ]
    if active_profile is None:
        recommendations.append(_recommendation(
            key="company-profile",
            title="Configure Organization",
            label="Required",
            description="Confirm the website and federal identifiers BidLens cannot reliably infer.",
            cta_label="Organization",
            cta_url=_workspace_url("/company-profile", organization_id),
            priority=10,
        ))
    else:
        completed.append(_completed_item(
            key="company-profile",
            title="Organization configured",
            completed_at=active_profile.updated_at,
        ))

    if not source_labels:
        recommendations.append(_recommendation(
            key="opportunity-source",
            title="Enable Opportunity Discovery",
            label="Required",
            description="Tell BidLens where to discover opportunities for this workspace.",
            cta_label="Opportunity Discovery",
            cta_url=_workspace_url("/opportunity-discovery", organization_id),
            priority=20,
        ))
    else:
        completed.append(_completed_item(
            key="opportunity-source",
            title="Opportunity Discovery enabled",
            completed_at=last_import_at,
            description="BidLens has at least one source to monitor for opportunities.",
        ))

    if member_count <= 1:
        recommendations.append(_recommendation(
            key="invite-team",
            title="Invite your team",
            label="Recommended",
            description="Allow teammates to review and qualify opportunities together.",
            cta_label="Workspace Members",
            cta_url=_workspace_url(f"/admin/organizations/{organization_id}/users", organization_id),
            priority=30,
        ))
    else:
        completed.append(_completed_item(
            key="invite-team",
            title="Users invited",
            description="More than one team member can use this workspace.",
        ))

    if not salesforce_connected:
        recommendations.append(_recommendation(
            key="business-systems",
            title="Connect Business Systems",
            label="Optional",
            description="Send BidLens opportunities, decisions, and updates to the systems your team already uses.",
            cta_label="Outbound Integrations",
            cta_url=_workspace_url("/outbound-integrations", organization_id),
            priority=40,
        ))
    else:
        completed.append(_completed_item(
            key="business-systems",
            title="Business systems connected",
        ))

    if not _has_setup_event(db, organization_id, "feed_rules_configured"):
        recommendations.append(_recommendation(
            key="feed-rules",
            title="Configure Feed Rules",
            label="Recommended",
            description="Set the workspace defaults BidLens uses to match and route incoming opportunities.",
            cta_label="Feed Rules",
            cta_url=_workspace_url("/settings", organization_id),
            priority=45,
        ))
    else:
        completed.append(_completed_item(
            key="feed-rules",
            title="Feed rules configured",
        ))

    if lane_count == 0:
        recommendations.append(_recommendation(
            key="pursuit-lanes",
            title="Configure pursuit lanes",
            label="Recommended",
            description="Helps organize opportunities by strategic markets, service areas, or teams.",
            cta_label="Pursuit Lanes",
            cta_url=_workspace_url("/pursuit-lanes", organization_id),
            priority=50,
        ))
    else:
        completed.append(_completed_item(
            key="pursuit-lanes",
            title="Pursuit lanes configured",
        ))

    recommendations.sort(key=lambda item: item["priority"])
    required_setup_complete = not any(
        item["label"] == "Required" for item in recommendations
    )
    is_live = bool(organization.is_live)
    snapshot_cards = [
        {
            "key": "sources",
            "title": "Opportunity sources enabled",
            "value": len(source_labels),
            "detail": "Opportunity intake is configured" if source_labels else "No sources configured",
            "tone": "neutral" if source_labels else "attention",
            "url": _workspace_url("/integrations", organization_id),
        },
        {
            "key": "last-import",
            "title": "Last successful import",
            "value": last_import_at,
            "detail": "Latest intake completed" if last_successful_import else "No successful imports yet",
            "tone": "neutral",
            "url": _workspace_url("/imports/history", organization_id),
        },
        {
            "key": "awaiting-review",
            "title": "Opportunities awaiting review",
            "value": awaiting_review_count,
            "detail": "Ready for a team decision" if awaiting_review_count else "Review queue is clear",
            "tone": "attention" if awaiting_review_count else "healthy",
            "url": _workspace_url("/", organization_id),
        },
        {
            "key": "recent-updates",
            "title": "Recent updates detected",
            "value": recent_update_count,
            "detail": "In the last 7 days",
            "tone": "attention" if recent_update_count else "neutral",
            "url": _workspace_url("/admin/source-updates", organization_id),
        },
        {
            "key": "salesforce",
            "title": "CRM connection",
            "value": "Connected" if salesforce_connected else "Not connected",
            "detail": "Optional CRM integration",
            "tone": "healthy" if salesforce_connected else "neutral",
            "url": _workspace_url("/integrations", organization_id, fragment="salesforce"),
        },
        {
            "key": "connector-issues",
            "title": "Connector issues",
            "value": len(attention_items),
            "detail": "Needs attention" if attention_items else "No issues detected",
            "tone": "attention" if attention_items else "healthy",
            "url": _workspace_url("/integrations", organization_id),
        },
    ]

    return {
        "workspace_summary": {
            "organization_id": organization.id,
            "organization_name": organization.name,
            "headline": "Welcome to BidLens.",
            "description": (
                "Your workspace is ready."
                if is_live
                else "Let’s get your organization ready."
            ),
            "required_setup_complete": required_setup_complete,
            "setup_complete": required_setup_complete,
            "is_live": is_live,
            "can_go_live": required_setup_complete and not is_live,
            "member_count": member_count,
            "opportunity_count": opportunity_count,
        },
        "welcome": {
            "headline": "Welcome to BidLens.",
            "description": (
                "Your workspace is ready."
                if is_live
                else "Let’s get your organization ready."
            ),
        },
        "is_live": is_live,
        "recommendations": recommendations,
        "setup_recommendations": recommendations,
        "next_steps": recommendations,
        "completed": completed,
        "completed_setup_items": completed,
        "setup_complete": required_setup_complete,
        "required_setup_complete": required_setup_complete,
        "can_go_live": required_setup_complete and not is_live,
        "setup_history": _setup_history_from_events(db, organization_id),
        "operational_snapshot": {
            "sources_enabled": len(source_labels),
            "source_labels": source_labels,
            "last_successful_import": last_import_at,
            "opportunities_awaiting_review": awaiting_review_count,
            "recent_updates_detected": recent_update_count,
            "salesforce_connected": bool(salesforce_connected),
            "connector_issues": len(attention_items),
            "cards": snapshot_cards,
        },
        "operational_home_context": {
            "workspace_summary": {
                "organization_id": organization.id,
                "organization_name": organization.name,
                "member_count": member_count,
                "opportunity_count": opportunity_count,
            },
            "operational_snapshot": {
                "sources_enabled": len(source_labels),
                "source_labels": source_labels,
                "last_successful_import": last_import_at,
                "opportunities_awaiting_review": awaiting_review_count,
                "recent_updates_detected": recent_update_count,
                "salesforce_connected": bool(salesforce_connected),
                "connector_issues": len(attention_items),
                "cards": snapshot_cards,
            },
            "attention_items": attention_items,
        } if is_live else None,
        "attention_items": attention_items,
        "current_user_id": user_id,
        "daily_snapshot": daily_snapshot,
    }
