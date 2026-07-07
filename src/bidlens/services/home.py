from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import (
    CompanyProfile,
    Event,
    IngestionRun,
    Opportunity,
    OpportunityUpdateEvent,
    Organization,
    OrganizationMembership,
    OrgProfile,
    PursuitLane,
    SamSourceConfig,
)
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
    "company_profile_configured": "Organization profile configured",
    "opportunity_sources_connected": "Opportunity sources connected",
    "first_successful_import": "First successful import",
    "users_invited": "Users invited",
    "salesforce_connected": "CRM connected",
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
        salesforce_connected = SalesforceService().has_stored_authorization

    last_import_at = (
        (last_successful_import.finished_at or last_successful_import.started_at)
        if last_successful_import
        else None
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
            title="Tell BidLens about your organization",
            label="Required",
            description="Helps BidLens understand your capabilities, markets, and strategic focus.",
            cta_label="Company Profile",
            cta_url=_workspace_url("/company-profile", organization_id),
            priority=10,
        ))
    else:
        completed.append(_completed_item(
            key="company-profile",
            title="Organization profile configured",
            completed_at=active_profile.updated_at,
        ))

    if not source_labels:
        recommendations.append(_recommendation(
            key="opportunity-source",
            title="Enable at least one opportunity source",
            label="Required",
            description="Allows BidLens to receive opportunities for your organization.",
            cta_label="Opportunity Sources",
            cta_url=_workspace_url("/integrations", organization_id),
            priority=20,
        ))
    else:
        completed.append(_completed_item(
            key="opportunity-source",
            title="Opportunity sources connected",
            completed_at=last_import_at,
            description="Opportunity intake is configured.",
        ))

    if member_count <= 1:
        recommendations.append(_recommendation(
            key="invite-team",
            title="Invite your team",
            label="Recommended",
            description="Allow teammates to review and qualify opportunities together.",
            cta_label="User Administration",
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
            key="salesforce",
            title="Connect your CRM",
            label="Optional",
            description="Allows BidLens to create and update opportunities in your CRM.",
            cta_label="Salesforce Configuration",
            cta_url=_workspace_url("/integrations", organization_id, fragment="salesforce"),
            priority=40,
        ))
    else:
        completed.append(_completed_item(
            key="salesforce",
            title="CRM connected",
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
    }
