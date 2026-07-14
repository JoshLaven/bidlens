from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import (
    DailySnapshot,
    Event,
    IngestionRun,
    Opportunity,
    OpportunityPursuitLaneMatch,
    OpportunityUpdateEvent,
    User,
    Workspace,
)
from .pursuit_lanes import user_my_lanes


SNAPSHOT_VERSION = "daily_snapshot_v1"
DEFAULT_DEADLINE_WINDOW_DAYS = 7
ISSUE_RUN_STATUSES = ("failed", "error", "partial_success")
SOURCE_LABELS = {
    "sam": "SAM.gov",
    "sam.gov": "SAM.gov",
    "grants_gov": "Grants.gov",
    "grants.gov": "Grants.gov",
    "govwin_api": "GovWin",
    "govwin_export": "GovWin",
}


def _source_label(source: str | None) -> str:
    normalized = str(source or "").strip().lower()
    return SOURCE_LABELS.get(normalized, str(source or "Unknown source"))


def _activity_date(snapshot_date: dt.date) -> dt.date:
    return snapshot_date - dt.timedelta(days=1)


def _day_window(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime.combine(day, dt.time.min)
    return start, start + dt.timedelta(days=1)


def _iso_datetime(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


def _iso_date(value: dt.date | None) -> str | None:
    return value.isoformat() if value else None


def _opportunity_payload(opportunity: Opportunity) -> dict[str, Any]:
    return {
        "id": opportunity.id,
        "bidlens_id": str(opportunity.bidlens_id) if opportunity.bidlens_id else None,
        "title": opportunity.title,
        "agency": opportunity.agency,
        "source": opportunity.source,
        "source_label": _source_label(opportunity.source),
        "source_record_id": opportunity.source_record_id,
        "opportunity_type": opportunity.opportunity_type,
        "source_stage": opportunity.source_stage,
        "posted_date": _iso_date(opportunity.posted_date),
        "response_deadline": _iso_date(opportunity.response_deadline),
        "decision_state": opportunity.decision_state,
        "qualification_status": opportunity.qualification_status,
        "url": opportunity.source_url or opportunity.sam_url,
    }


def _event_user_payload(db: Session, user_id: int | None) -> dict[str, Any] | None:
    if not user_id:
        return None
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return {"id": user_id, "name": None, "email": None}
    return {"id": user.id, "name": user.name, "email": user.email}


def _new_opportunities(
    db: Session,
    *,
    organization_id: int,
    start_at: dt.datetime,
    end_before: dt.datetime,
) -> list[dict[str, Any]]:
    rows = (
        db.query(Opportunity)
        .filter(
            Opportunity.organization_id == organization_id,
            Opportunity.created_at >= start_at,
            Opportunity.created_at < end_before,
        )
        .order_by(Opportunity.created_at.asc(), Opportunity.id.asc())
        .limit(50)
        .all()
    )
    return [
        {
            **_opportunity_payload(opportunity),
            "created_at": _iso_datetime(opportunity.created_at),
        }
        for opportunity in rows
    ]


def _updated_opportunities(
    db: Session,
    *,
    organization_id: int,
    start_at: dt.datetime,
    end_before: dt.datetime,
) -> list[dict[str, Any]]:
    rows = (
        db.query(OpportunityUpdateEvent, Opportunity)
        .join(Opportunity, Opportunity.id == OpportunityUpdateEvent.opportunity_id)
        .filter(
            OpportunityUpdateEvent.organization_id == organization_id,
            OpportunityUpdateEvent.detected_at >= start_at,
            OpportunityUpdateEvent.detected_at < end_before,
        )
        .order_by(OpportunityUpdateEvent.detected_at.asc(), OpportunityUpdateEvent.id.asc())
        .limit(50)
        .all()
    )
    return [
        {
            "event_id": event.id,
            "detected_at": _iso_datetime(event.detected_at),
            "changed_fields": event.changed_fields or {},
            "salesforce_sync_status": event.salesforce_sync_status,
            "opportunity": _opportunity_payload(opportunity),
        }
        for event, opportunity in rows
    ]


def _shortlist_changes(
    db: Session,
    *,
    organization_id: int,
    start_at: dt.datetime,
    end_before: dt.datetime,
) -> list[dict[str, Any]]:
    rows = (
        db.query(Event, Opportunity)
        .join(Opportunity, Opportunity.id == Event.opp_id)
        .filter(
            Event.org_id == organization_id,
            Event.event_type == "state_changed",
            Event.ts >= start_at,
            Event.ts < end_before,
        )
        .order_by(Event.ts.asc(), Event.id.asc())
        .limit(50)
        .all()
    )
    changes = []
    for event, opportunity in rows:
        payload = event.payload or {}
        if payload.get("to") != "SHORTLISTED" and payload.get("from") != "SHORTLISTED":
            continue
        changes.append(
            {
                "event_id": event.id,
                "occurred_at": _iso_datetime(event.ts),
                "from": payload.get("from"),
                "to": payload.get("to"),
                "user": _event_user_payload(db, event.user_id),
                "opportunity": _opportunity_payload(opportunity),
            }
        )
    return changes


def _upcoming_deadlines(
    db: Session,
    *,
    organization_id: int,
    snapshot_date: dt.date,
    days: int = DEFAULT_DEADLINE_WINDOW_DAYS,
) -> list[dict[str, Any]]:
    end_date = snapshot_date + dt.timedelta(days=days)
    rows = (
        db.query(Opportunity)
        .filter(
            Opportunity.organization_id == organization_id,
            Opportunity.decision_state != "ARCHIVED",
            Opportunity.response_deadline >= snapshot_date,
            Opportunity.response_deadline <= end_date,
        )
        .order_by(Opportunity.response_deadline.asc(), Opportunity.id.asc())
        .limit(50)
        .all()
    )
    return [
        {
            **_opportunity_payload(opportunity),
            "days_until_deadline": (opportunity.response_deadline - snapshot_date).days
            if opportunity.response_deadline
            else None,
        }
        for opportunity in rows
    ]


def _interested_activity(
    db: Session,
    *,
    organization_id: int,
    start_at: dt.datetime,
    end_before: dt.datetime,
) -> list[dict[str, Any]]:
    rows = (
        db.query(Event, Opportunity)
        .join(Opportunity, Opportunity.id == Event.opp_id)
        .filter(
            Event.org_id == organization_id,
            Event.event_type == "vote_cast",
            Event.ts >= start_at,
            Event.ts < end_before,
        )
        .order_by(Event.ts.asc(), Event.id.asc())
        .limit(50)
        .all()
    )
    activity = []
    for event, opportunity in rows:
        payload = event.payload or {}
        if payload.get("requested_vote") != "PURSUE" and payload.get("vote") != "PURSUE":
            continue
        activity.append(
            {
                "event_id": event.id,
                "occurred_at": _iso_datetime(event.ts),
                "vote": payload.get("vote"),
                "toggled_off": bool(payload.get("toggled_off")),
                "user": _event_user_payload(db, event.user_id),
                "opportunity": _opportunity_payload(opportunity),
            }
        )
    return activity


def _connector_issues(
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
        latest_by_source.setdefault(_source_label(run.source), run)

    issue_rows = []
    for label, run in sorted(latest_by_source.items()):
        normalized_status = str(run.status or "").strip().lower()
        needs_attention = normalized_status in ISSUE_RUN_STATUSES or bool(run.error_count)
        if not needs_attention:
            continue
        issue_rows.append(
            {
                "source": run.source,
                "source_label": label,
                "status": run.status,
                "started_at": _iso_datetime(run.started_at),
                "finished_at": _iso_datetime(run.finished_at),
                "processed_count": run.processed_count,
                "created_count": run.created_count,
                "updated_count": run.updated_count,
                "error_count": run.error_count,
                "needs_attention": True,
                "notes": run.notes,
            }
        )
    return issue_rows


def _my_lane_context(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    start_at: dt.datetime,
    end_before: dt.datetime,
    snapshot_date: dt.date,
) -> list[dict[str, Any]]:
    lanes = user_my_lanes(db, organization_id=organization_id, user_id=user_id)
    if not lanes:
        return []

    deadline_end = snapshot_date + dt.timedelta(days=DEFAULT_DEADLINE_WINDOW_DAYS)
    context = []
    for lane in lanes:
        new_count = (
            db.query(func.count(func.distinct(Opportunity.id)))
            .join(
                OpportunityPursuitLaneMatch,
                OpportunityPursuitLaneMatch.opportunity_id == Opportunity.id,
            )
            .filter(
                Opportunity.organization_id == organization_id,
                OpportunityPursuitLaneMatch.organization_id == organization_id,
                OpportunityPursuitLaneMatch.pursuit_lane_id == lane.id,
                Opportunity.created_at >= start_at,
                Opportunity.created_at < end_before,
            )
            .scalar()
            or 0
        )
        updated_count = (
            db.query(func.count(func.distinct(OpportunityUpdateEvent.opportunity_id)))
            .join(Opportunity, Opportunity.id == OpportunityUpdateEvent.opportunity_id)
            .join(
                OpportunityPursuitLaneMatch,
                OpportunityPursuitLaneMatch.opportunity_id == Opportunity.id,
            )
            .filter(
                OpportunityUpdateEvent.organization_id == organization_id,
                OpportunityPursuitLaneMatch.organization_id == organization_id,
                OpportunityPursuitLaneMatch.pursuit_lane_id == lane.id,
                OpportunityUpdateEvent.detected_at >= start_at,
                OpportunityUpdateEvent.detected_at < end_before,
            )
            .scalar()
            or 0
        )
        deadline_count = (
            db.query(func.count(func.distinct(Opportunity.id)))
            .join(
                OpportunityPursuitLaneMatch,
                OpportunityPursuitLaneMatch.opportunity_id == Opportunity.id,
            )
            .filter(
                Opportunity.organization_id == organization_id,
                Opportunity.decision_state != "ARCHIVED",
                Opportunity.response_deadline >= snapshot_date,
                Opportunity.response_deadline <= deadline_end,
                OpportunityPursuitLaneMatch.organization_id == organization_id,
                OpportunityPursuitLaneMatch.pursuit_lane_id == lane.id,
            )
            .scalar()
            or 0
        )
        context.append(
            {
                "id": lane.id,
                "name": lane.name,
                "new_opportunity_count": new_count,
                "updated_opportunity_count": updated_count,
                "upcoming_deadline_count": deadline_count,
            }
        )
    return context


def build_snapshot_payload(
    db: Session,
    *,
    workspace: Workspace,
    user_id: int,
    snapshot_date: dt.date,
) -> dict[str, Any]:
    activity_day = _activity_date(snapshot_date)
    start_at, end_before = _day_window(activity_day)
    organization_id = workspace.organization_id

    my_lane_context = _my_lane_context(
        db,
        organization_id=organization_id,
        user_id=user_id,
        start_at=start_at,
        end_before=end_before,
        snapshot_date=snapshot_date,
    )

    return {
        "version": SNAPSHOT_VERSION,
        "workspace": {
            "id": workspace.id,
            "organization_id": organization_id,
            "name": workspace.name,
        },
        "user": _event_user_payload(db, user_id),
        "snapshot_date": snapshot_date.isoformat(),
        "activity_date": activity_day.isoformat(),
        "activity_window": {
            "start": start_at.isoformat(),
            "end": end_before.isoformat(),
            "basis": "calendar_day",
        },
        "my_shortlist": [],
        "team_signals": [],
        "my_lanes": [],
        "my_lane_context": my_lane_context,
        "new_opportunities": _new_opportunities(
            db,
            organization_id=organization_id,
            start_at=start_at,
            end_before=end_before,
        ),
        "updated_opportunities": _updated_opportunities(
            db,
            organization_id=organization_id,
            start_at=start_at,
            end_before=end_before,
        ),
        "upcoming_deadlines": _upcoming_deadlines(
            db,
            organization_id=organization_id,
            snapshot_date=snapshot_date,
        ),
        "interested_activity": _interested_activity(
            db,
            organization_id=organization_id,
            start_at=start_at,
            end_before=end_before,
        ),
        "shortlist_changes": _shortlist_changes(
            db,
            organization_id=organization_id,
            start_at=start_at,
            end_before=end_before,
        ),
        "connector_issues": _connector_issues(db, organization_id=organization_id),
    }


def get_stored_daily_snapshot(
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


def create_daily_snapshot(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    snapshot_date: dt.date | None = None,
) -> DailySnapshot:
    snapshot_date = snapshot_date or dt.date.today()
    existing = get_stored_daily_snapshot(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        snapshot_date=snapshot_date,
    )
    if existing:
        return existing

    workspace = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not workspace:
        raise ValueError(f"Workspace {workspace_id} not found")

    payload = build_snapshot_payload(
        db,
        workspace=workspace,
        user_id=user_id,
        snapshot_date=snapshot_date,
    )
    snapshot = DailySnapshot(
        workspace_id=workspace.id,
        user_id=user_id,
        snapshot_date=snapshot_date,
        status="completed",
        snapshot_json=payload,
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot
