from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from bidlens.database import SessionLocal
from bidlens.models import (
    DailySnapshot,
    Event,
    Opportunity,
    OpportunityUpdateEvent,
    Organization,
    OrganizationMembership,
    User,
    Vote,
    Workspace,
)
from bidlens.services.daily_snapshot import create_daily_snapshot, get_stored_daily_snapshot


SECTION_LABELS = [
    ("my_shortlist", "My Shortlist"),
    ("shortlist_updates", "Shortlist Updates"),
    ("shortlist_deadlines", "Shortlist Deadlines"),
    ("team_signals", "Team Signals"),
    ("my_lanes", "My Lanes"),
    ("new_opportunities", "New Opportunities"),
    ("updated_opportunities", "Updated Opportunities"),
    ("upcoming_deadlines", "Upcoming Deadlines"),
    ("interested_activity", "Interested Activity"),
    ("shortlist_changes", "Shortlist Changes"),
    ("connector_issues", "Connector Issues"),
]


def _parse_date(value: str | None) -> dt.date:
    if not value:
        return dt.date.today()
    return dt.date.fromisoformat(value)


def _day_window(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime.combine(day, dt.time.min)
    return start, start + dt.timedelta(days=1)


def _display_user(user: User | None) -> str:
    if not user:
        return "Unknown user"
    return user.name or user.email or f"User {user.id}"


def _opportunity_title(item: dict[str, Any]) -> str:
    opportunity = item.get("opportunity") if isinstance(item, dict) else None
    if isinstance(opportunity, dict):
        return str(opportunity.get("title") or "Untitled opportunity")
    return str(item.get("title") or "Untitled opportunity")


def _format_section_item(section_key: str, item: dict[str, Any]) -> str:
    if section_key in {"my_shortlist", "shortlist_updates", "shortlist_deadlines", "team_signals", "my_lanes"}:
        parts = [str(item.get("title") or "Untitled update")]
        if item.get("subtitle"):
            parts.append(str(item["subtitle"]))
        if item.get("destination_url"):
            parts.append(str(item["destination_url"]))
        return " · ".join(parts)

    if section_key == "new_opportunities":
        parts = [_opportunity_title(item)]
        if item.get("agency"):
            parts.append(str(item["agency"]))
        if item.get("response_deadline"):
            parts.append(f"due {item['response_deadline']}")
        return " · ".join(parts)

    if section_key == "updated_opportunities":
        fields = item.get("changed_fields") or {}
        field_names = ", ".join(fields.keys()) if isinstance(fields, dict) else ""
        return f"{_opportunity_title(item)}" + (f" · {field_names}" if field_names else "")

    if section_key == "upcoming_deadlines":
        parts = [_opportunity_title(item)]
        if item.get("response_deadline"):
            parts.append(f"due {item['response_deadline']}")
        if item.get("days_until_deadline") is not None:
            parts.append(f"{item['days_until_deadline']} days out")
        return " · ".join(parts)

    if section_key == "interested_activity":
        user = item.get("user") or {}
        actor = user.get("name") or user.get("email") or "Someone"
        state = "removed interest" if item.get("toggled_off") else "showed interest"
        return f"{actor} {state} · {_opportunity_title(item)}"

    if section_key == "shortlist_changes":
        user = item.get("user") or {}
        actor = user.get("name") or user.get("email") or "Someone"
        detail = item.get("subtitle") or item.get("change_type") or "changed"
        return f"{actor} · {_opportunity_title(item)} · {detail}"

    if section_key == "connector_issues":
        parts = [str(item.get("source_label") or item.get("source") or "Unknown source")]
        if item.get("status"):
            parts.append(str(item["status"]))
        if item.get("notes"):
            parts.append(str(item["notes"]))
        return " · ".join(parts)

    return json.dumps(item, sort_keys=True)


def format_snapshot(snapshot: DailySnapshot) -> str:
    payload = snapshot.snapshot_json or {}
    workspace = payload.get("workspace") or {}
    user = payload.get("user") or {}
    lines = [
        "Daily Snapshot",
        "",
        f"Date: {snapshot.snapshot_date.isoformat()}",
        f"Activity Date: {payload.get('activity_date') or 'Unknown'}",
        f"Workspace: {workspace.get('name') or snapshot.workspace_id}",
        f"User: {user.get('name') or user.get('email') or snapshot.user_id}",
        f"Status: {snapshot.status}",
        "-" * 32,
    ]

    my_lane_context = payload.get("my_lane_context") or []
    lines.extend(["", "My Lane Context"])
    if my_lane_context:
        for lane in my_lane_context:
            lines.append(
                "  - "
                f"{lane.get('name') or lane.get('id')} · "
                f"{lane.get('new_opportunity_count', 0)} new · "
                f"{lane.get('updated_opportunity_count', 0)} updated · "
                f"{lane.get('upcoming_deadline_count', 0)} deadlines"
            )
    else:
        lines.append("  (none)")

    for section_key, label in SECTION_LABELS:
        lines.extend(["", label])
        items = payload.get(section_key) or []
        if not items:
            lines.append("  (none)")
            continue
        for item in items:
            lines.append(f"  - {_format_section_item(section_key, item)}")

    return "\n".join(lines)


def inspect_snapshot(snapshot_id: int, *, as_json: bool = False) -> int:
    db = SessionLocal()
    try:
        snapshot = db.query(DailySnapshot).filter(DailySnapshot.id == snapshot_id).first()
        if not snapshot:
            print(f"Daily Snapshot {snapshot_id} not found.")
            return 1
        if as_json:
            print(json.dumps(snapshot.snapshot_json or {}, indent=2, sort_keys=True))
        else:
            print(format_snapshot(snapshot))
        return 0
    finally:
        db.close()


def _workspace_or_default(db, workspace_id: int | None = None) -> Workspace | None:
    query = db.query(Workspace)
    if workspace_id:
        query = query.filter(Workspace.id == workspace_id)
    return query.order_by(Workspace.id.asc()).first()


def _user_or_default(db, workspace: Workspace, user_email: str | None = None) -> User | None:
    query = (
        db.query(User)
        .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
        .filter(OrganizationMembership.organization_id == workspace.organization_id)
    )
    if user_email:
        query = query.filter(User.email == user_email)
    return query.order_by(User.email.asc(), User.id.asc()).first()


def _teammate_or_create(db, workspace: Workspace, user: User) -> User:
    teammate = (
        db.query(User)
        .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
        .filter(
            OrganizationMembership.organization_id == workspace.organization_id,
            User.id != user.id,
        )
        .order_by(User.email.asc(), User.id.asc())
        .first()
    )
    if teammate:
        return teammate

    teammate = User(
        email=f"daily-brief-teammate-{workspace.organization_id}@example.test",
        name="Daily Brief Teammate",
        organization_id=workspace.organization_id,
    )
    db.add(teammate)
    db.flush()
    db.add(OrganizationMembership(
        organization_id=workspace.organization_id,
        user_id=teammate.id,
        role="member",
    ))
    db.flush()
    return teammate


def _qa_opportunity(
    db,
    *,
    workspace: Workspace,
    record_id: str,
    title: str,
    created_at: dt.datetime,
    response_deadline: dt.date | None = None,
) -> Opportunity:
    existing = (
        db.query(Opportunity)
        .filter(
            Opportunity.organization_id == workspace.organization_id,
            Opportunity.source == "daily_snapshot_qa",
            Opportunity.source_record_id == record_id,
        )
        .first()
    )
    if existing:
        existing.title = title
        existing.created_at = created_at
        existing.posted_date = created_at.date()
        existing.response_deadline = response_deadline or created_at.date() + dt.timedelta(days=14)
        existing.qualification_status = "qualified"
        existing.decision_state = "INBOX"
        return existing

    opportunity = Opportunity(
        organization_id=workspace.organization_id,
        source="daily_snapshot_qa",
        source_record_id=record_id,
        title=title,
        agency="Daily Brief QA Agency",
        opportunity_type="Solicitation",
        posted_date=created_at.date(),
        response_deadline=response_deadline or created_at.date() + dt.timedelta(days=14),
        qualification_status="qualified",
        decision_state="INBOX",
        created_at=created_at,
    )
    db.add(opportunity)
    db.flush()
    return opportunity


def _add_update(db, *, workspace: Workspace, opportunity: Opportunity, detected_at: dt.datetime, field: str = "response_deadline") -> None:
    db.add(OpportunityUpdateEvent(
        organization_id=workspace.organization_id,
        opportunity_id=opportunity.id,
        source=opportunity.source,
        source_record_id=opportunity.source_record_id,
        detected_at=detected_at,
        changed_fields={field: {"old": "before", "new": "after"}},
        salesforce_sync_status="not_synced",
    ))


def _add_vote_event(
    db,
    *,
    workspace: Workspace,
    user: User,
    opportunity: Opportunity,
    occurred_at: dt.datetime,
    requested_vote: str = "PURSUE",
    vote: str | None = "PURSUE",
    toggled_off: bool = False,
) -> None:
    row = (
        db.query(Vote)
        .filter(
            Vote.org_id == workspace.organization_id,
            Vote.user_id == user.id,
            Vote.opp_id == opportunity.id,
        )
        .first()
    )
    if row is None:
        row = Vote(
            org_id=workspace.organization_id,
            user_id=user.id,
            opp_id=opportunity.id,
        )
        db.add(row)
    row.vote = vote
    row.updated_at = occurred_at
    db.add(Event(
        org_id=workspace.organization_id,
        user_id=user.id,
        opp_id=opportunity.id,
        event_type="vote_cast",
        ui_version="daily_snapshot_qa",
        ts=occurred_at,
        payload={
            "vote": vote,
            "requested_vote": requested_vote,
            "toggled_off": toggled_off,
            "qa_seed": True,
        },
    ))


def seed_qa_scenario(
    db,
    *,
    scenario: str,
    snapshot_date: dt.date,
    workspace_id: int | None = None,
    user_email: str | None = None,
) -> dict[str, object]:
    workspace = _workspace_or_default(db, workspace_id)
    if not workspace:
        raise ValueError("No workspace found. Provision a workspace before seeding Daily Brief QA data.")
    user = _user_or_default(db, workspace, user_email)
    if not user:
        raise ValueError("No workspace member found for the selected workspace.")

    activity_day = snapshot_date - dt.timedelta(days=1)
    start_at, end_before = _day_window(activity_day)
    in_window = start_at + dt.timedelta(hours=10)
    outside_window = end_before + dt.timedelta(hours=2)
    teammate = _teammate_or_create(db, workspace, user)
    seeded: list[str] = []

    def opportunity(suffix: str, title: str, when: dt.datetime = in_window) -> Opportunity:
        return _qa_opportunity(
            db,
            workspace=workspace,
            record_id=f"QA-{snapshot_date.isoformat()}-{scenario}-{suffix}",
            title=title,
            created_at=when,
        )

    if scenario in {"updated", "multiple-signals", "all"}:
        opp = opportunity("UPDATED", "QA Updated Opportunity")
        _add_update(db, workspace=workspace, opportunity=opp, detected_at=in_window, field="response_deadline")
        _add_update(db, workspace=workspace, opportunity=opp, detected_at=in_window + dt.timedelta(minutes=5), field="title")
        _add_vote_event(db, workspace=workspace, user=user, opportunity=opp, occurred_at=start_at - dt.timedelta(days=1), vote="PURSUE")
        seeded.append("updated opportunity")

    if scenario in {"shortlist-add", "multiple-signals", "all"}:
        opp = opportunity("SHORTLIST-ADD", "QA Shortlist Addition")
        _add_vote_event(db, workspace=workspace, user=user, opportunity=opp, occurred_at=in_window, vote="PURSUE")
        seeded.append("current-user shortlist addition")

    if scenario in {"shortlist-remove", "all"}:
        opp = opportunity("SHORTLIST-REMOVE", "QA Shortlist Removal")
        _add_vote_event(db, workspace=workspace, user=user, opportunity=opp, occurred_at=in_window, requested_vote="PURSUE", vote=None, toggled_off=True)
        seeded.append("current-user shortlist removal")

    if scenario in {"teammate-interest", "multiple-signals", "all"}:
        opp = opportunity("TEAMMATE", "QA Teammate Interest")
        _add_vote_event(db, workspace=workspace, user=user, opportunity=opp, occurred_at=start_at - dt.timedelta(days=1), vote="PURSUE")
        _add_vote_event(db, workspace=workspace, user=teammate, opportunity=opp, occurred_at=in_window, vote="PURSUE")
        seeded.append("teammate interest on tracked opportunity")

    if scenario in {"out-of-window", "all"}:
        opp = opportunity("OUTSIDE", "QA Out Of Window Activity", outside_window)
        _add_update(db, workspace=workspace, opportunity=opp, detected_at=outside_window)
        _add_vote_event(db, workspace=workspace, user=user, opportunity=opp, occurred_at=outside_window, vote="PURSUE")
        seeded.append("out-of-window activity")

    if scenario in {"cross-workspace", "all"}:
        other_slug = f"daily-brief-qa-other-{snapshot_date.isoformat()}"
        other_org = db.query(Organization).filter(Organization.slug == other_slug).first()
        if other_org is None:
            other_org = Organization(
                name=f"Daily Brief QA Other {snapshot_date.isoformat()}",
                slug=other_slug,
            )
            db.add(other_org)
            db.flush()
        other_workspace = (
            db.query(Workspace)
            .filter(Workspace.organization_id == other_org.id)
            .order_by(Workspace.id.asc())
            .first()
        )
        if other_workspace is None:
            other_workspace = Workspace(
                organization_id=other_org.id,
                name="Daily Brief QA Other Workspace",
                slug=f"daily-brief-qa-other-workspace-{snapshot_date.isoformat()}",
            )
            db.add(other_workspace)
            db.flush()
        other_email = f"daily-brief-other-{snapshot_date.isoformat()}@example.test"
        other_user = db.query(User).filter(User.email == other_email).first()
        if other_user is None:
            other_user = User(
                email=other_email,
                name="Daily Brief Other User",
                organization_id=other_org.id,
            )
            db.add(other_user)
            db.flush()
        if not db.query(OrganizationMembership.id).filter(
            OrganizationMembership.organization_id == other_org.id,
            OrganizationMembership.user_id == other_user.id,
        ).first():
            db.add(OrganizationMembership(
                organization_id=other_org.id,
                user_id=other_user.id,
                role="member",
            ))
        other_opp = _qa_opportunity(
            db,
            workspace=other_workspace,
            record_id=f"QA-{snapshot_date.isoformat()}-{scenario}-OTHER",
            title="QA Cross Workspace Opportunity",
            created_at=in_window,
        )
        _add_update(db, workspace=other_workspace, opportunity=other_opp, detected_at=in_window)
        seeded.append("cross-workspace activity")

    db.commit()
    return {
        "scenario": scenario,
        "snapshot_date": snapshot_date.isoformat(),
        "activity_date": activity_day.isoformat(),
        "workspace_id": workspace.id,
        "workspace": workspace.name,
        "user_id": user.id,
        "user": _display_user(user),
        "seeded": seeded,
    }


def seed_qa_scenario_command(
    scenario: str,
    *,
    snapshot_date: dt.date,
    workspace_id: int | None = None,
    user_email: str | None = None,
) -> int:
    db = SessionLocal()
    try:
        result = seed_qa_scenario(
            db,
            scenario=scenario,
            snapshot_date=snapshot_date,
            workspace_id=workspace_id,
            user_email=user_email,
        )
        print("Daily Snapshot QA Seed")
        print("")
        print(f"Scenario: {result['scenario']}")
        print(f"Snapshot Date: {result['snapshot_date']}")
        print(f"Activity Date: {result['activity_date']}")
        print(f"Workspace: {result['workspace']} ({result['workspace_id']})")
        print(f"User: {result['user']} ({result['user_id']})")
        print("")
        print("Seeded:")
        for item in result["seeded"]:
            print(f"- {item}")
        return 0
    except Exception as exc:
        print(f"Daily Snapshot QA seed failed: {exc}")
        return 1
    finally:
        db.close()


def _snapshot_lookup(db, *, snapshot_id: int | None, workspace_id: int | None, user_id: int | None, user_email: str | None, snapshot_date: dt.date | None) -> DailySnapshot | None:
    if snapshot_id:
        return db.query(DailySnapshot).filter(DailySnapshot.id == snapshot_id).first()
    if not workspace_id or not snapshot_date:
        return None
    resolved_user_id = user_id
    if not resolved_user_id and user_email:
        user = db.query(User).filter(User.email == user_email).first()
        resolved_user_id = user.id if user else None
    if not resolved_user_id:
        return None
    return get_stored_daily_snapshot(
        db,
        workspace_id=workspace_id,
        user_id=resolved_user_id,
        snapshot_date=snapshot_date,
    )


def inspect_snapshot_by_lookup(
    *,
    snapshot_id: int | None = None,
    workspace_id: int | None = None,
    user_id: int | None = None,
    user_email: str | None = None,
    snapshot_date: dt.date | None = None,
    as_json: bool = False,
) -> int:
    db = SessionLocal()
    try:
        snapshot = _snapshot_lookup(
            db,
            snapshot_id=snapshot_id,
            workspace_id=workspace_id,
            user_id=user_id,
            user_email=user_email,
            snapshot_date=snapshot_date,
        )
        if not snapshot:
            print("Daily Snapshot not found for the supplied lookup.")
            return 1
        if as_json:
            print(json.dumps(snapshot.snapshot_json or {}, indent=2, sort_keys=True))
        else:
            print(format_snapshot(snapshot))
        return 0
    finally:
        db.close()


def reset_snapshot(
    *,
    snapshot_id: int | None = None,
    workspace_id: int | None = None,
    user_id: int | None = None,
    user_email: str | None = None,
    snapshot_date: dt.date | None = None,
) -> int:
    db = SessionLocal()
    try:
        snapshot = _snapshot_lookup(
            db,
            snapshot_id=snapshot_id,
            workspace_id=workspace_id,
            user_id=user_id,
            user_email=user_email,
            snapshot_date=snapshot_date,
        )
        if not snapshot:
            print("No matching Daily Snapshot to delete.")
            return 1
        deleted = {
            "id": snapshot.id,
            "workspace_id": snapshot.workspace_id,
            "user_id": snapshot.user_id,
            "snapshot_date": snapshot.snapshot_date.isoformat(),
        }
        db.delete(snapshot)
        db.commit()
        print("Deleted Daily Snapshot")
        print(json.dumps(deleted, indent=2, sort_keys=True))
        return 0
    finally:
        db.close()


def cleanup_qa_data(
    *,
    snapshot_date: dt.date,
    workspace_id: int,
    user_id: int | None = None,
    user_email: str | None = None,
) -> int:
    db = SessionLocal()
    try:
        workspace = db.query(Workspace).filter(Workspace.id == workspace_id).first()
        if not workspace:
            print(f"Workspace {workspace_id} not found.")
            return 1
        resolved_user_id = user_id
        if not resolved_user_id and user_email:
            user = db.query(User).filter(User.email == user_email).first()
            resolved_user_id = user.id if user else None

        qa_prefix = f"QA-{snapshot_date.isoformat()}-"
        qa_opportunities = (
            db.query(Opportunity)
            .filter(
                Opportunity.organization_id == workspace.organization_id,
                Opportunity.source == "daily_snapshot_qa",
                Opportunity.source_record_id.like(f"{qa_prefix}%"),
            )
            .all()
        )
        qa_opp_ids = [opportunity.id for opportunity in qa_opportunities]
        deleted_counts = {
            "snapshots": 0,
            "events": 0,
            "votes": 0,
            "update_events": 0,
            "opportunities": 0,
            "qa_other_organizations": 0,
        }

        snapshot_query = db.query(DailySnapshot).filter(
            DailySnapshot.workspace_id == workspace.id,
            DailySnapshot.snapshot_date == snapshot_date,
        )
        if resolved_user_id:
            snapshot_query = snapshot_query.filter(DailySnapshot.user_id == resolved_user_id)
        deleted_counts["snapshots"] += snapshot_query.delete(synchronize_session=False)

        if qa_opp_ids:
            deleted_counts["events"] += (
                db.query(Event)
                .filter(Event.org_id == workspace.organization_id, Event.opp_id.in_(qa_opp_ids))
                .delete(synchronize_session=False)
            )
            deleted_counts["votes"] += (
                db.query(Vote)
                .filter(Vote.org_id == workspace.organization_id, Vote.opp_id.in_(qa_opp_ids))
                .delete(synchronize_session=False)
            )
            deleted_counts["update_events"] += (
                db.query(OpportunityUpdateEvent)
                .filter(
                    OpportunityUpdateEvent.organization_id == workspace.organization_id,
                    OpportunityUpdateEvent.opportunity_id.in_(qa_opp_ids),
                )
                .delete(synchronize_session=False)
            )
            deleted_counts["opportunities"] += (
                db.query(Opportunity)
                .filter(Opportunity.id.in_(qa_opp_ids))
                .delete(synchronize_session=False)
            )

        other_slug = f"daily-brief-qa-other-{snapshot_date.isoformat()}"
        other_org = db.query(Organization).filter(Organization.slug == other_slug).first()
        if other_org:
            other_workspaces = db.query(Workspace).filter(Workspace.organization_id == other_org.id).all()
            other_workspace_ids = [row.id for row in other_workspaces]
            other_opp_ids = [
                row.id
                for row in db.query(Opportunity.id).filter(Opportunity.organization_id == other_org.id).all()
            ]
            other_user_ids = [
                row.id
                for row in db.query(User.id).filter(User.organization_id == other_org.id).all()
            ]
            if other_opp_ids:
                deleted_counts["events"] += db.query(Event).filter(Event.opp_id.in_(other_opp_ids)).delete(synchronize_session=False)
                deleted_counts["votes"] += db.query(Vote).filter(Vote.opp_id.in_(other_opp_ids)).delete(synchronize_session=False)
                deleted_counts["update_events"] += (
                    db.query(OpportunityUpdateEvent)
                    .filter(OpportunityUpdateEvent.opportunity_id.in_(other_opp_ids))
                    .delete(synchronize_session=False)
                )
                deleted_counts["opportunities"] += (
                    db.query(Opportunity)
                    .filter(Opportunity.id.in_(other_opp_ids))
                    .delete(synchronize_session=False)
                )
            if other_workspace_ids:
                deleted_counts["snapshots"] += (
                    db.query(DailySnapshot)
                    .filter(DailySnapshot.workspace_id.in_(other_workspace_ids))
                    .delete(synchronize_session=False)
                )
                db.query(Workspace).filter(Workspace.id.in_(other_workspace_ids)).delete(synchronize_session=False)
            if other_user_ids:
                db.query(OrganizationMembership).filter(
                    OrganizationMembership.organization_id == other_org.id,
                    OrganizationMembership.user_id.in_(other_user_ids),
                ).delete(synchronize_session=False)
                db.query(User).filter(User.id.in_(other_user_ids)).delete(synchronize_session=False)
            db.delete(other_org)
            deleted_counts["qa_other_organizations"] += 1

        db.commit()
        print("Daily Snapshot QA Cleanup")
        print(json.dumps(deleted_counts, indent=2, sort_keys=True))
        return 0
    finally:
        db.close()


def generate_snapshots(snapshot_date: dt.date) -> int:
    db = SessionLocal()
    try:
        rows = (
            db.query(Workspace, OrganizationMembership, User)
            .join(Organization, Organization.id == Workspace.organization_id)
            .join(OrganizationMembership, OrganizationMembership.organization_id == Workspace.organization_id)
            .join(User, User.id == OrganizationMembership.user_id)
            .filter(Organization.is_active.is_(True))
            .order_by(Workspace.name.asc(), User.email.asc())
            .all()
        )

        generated: dict[str, list[str]] = defaultdict(list)
        skipped: dict[str, list[str]] = defaultdict(list)

        print("Daily Snapshot Generator")
        print("")

        for workspace, _membership, user in rows:
            existing = get_stored_daily_snapshot(
                db,
                workspace_id=workspace.id,
                user_id=user.id,
                snapshot_date=snapshot_date,
            )
            workspace_name = workspace.name
            user_name = _display_user(user)
            if existing:
                skipped[workspace_name].append(f"{user_name} (already exists)")
                continue

            create_daily_snapshot(
                db,
                workspace_id=workspace.id,
                user_id=user.id,
                snapshot_date=snapshot_date,
            )
            generated[workspace_name].append(user_name)

        workspace_names = sorted(set(generated) | set(skipped))
        if not workspace_names:
            print("No active workspace members found.")
            print("")
            print("Done.")
            return 0

        for workspace_name in workspace_names:
            print("Workspace:")
            print(workspace_name)
            print("")
            print("Generated:")
            for name in generated.get(workspace_name, []):
                print(name)
            if not generated.get(workspace_name):
                print("(none)")
            print("")
            print("Skipped:")
            for name in skipped.get(workspace_name, []):
                print(name)
            if not skipped.get(workspace_name):
                print("(none)")
            print("")

        print("Done.")
        return 0
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate or inspect Daily Snapshots.")
    parser.add_argument("--date", help="Snapshot date in YYYY-MM-DD format. Defaults to today.")
    parser.add_argument("--inspect", type=int, metavar="SNAPSHOT_ID", help="Inspect a stored Daily Snapshot by ID.")
    parser.add_argument("--inspect-date", action="store_true", help="Inspect a stored Daily Snapshot by --workspace-id, --user-id/--user-email, and --date.")
    parser.add_argument("--json", action="store_true", help="Print raw snapshot JSON when using --inspect.")
    parser.add_argument(
        "--seed-qa",
        choices=[
            "updated",
            "shortlist-add",
            "shortlist-remove",
            "teammate-interest",
            "multiple-signals",
            "out-of-window",
            "cross-workspace",
            "all",
        ],
        help="Seed development-only Daily Brief QA activity for the prior-day window of --date.",
    )
    parser.add_argument("--reset", action="store_true", help="Delete one stored Daily Snapshot by ID or workspace/user/date lookup.")
    parser.add_argument("--cleanup-qa", action="store_true", help="Delete Daily Brief QA seed data for --workspace-id and --date.")
    parser.add_argument("--workspace-id", type=int, help="Workspace ID for QA seed, inspect-date, or reset.")
    parser.add_argument("--user-id", type=int, help="User ID for inspect-date or reset.")
    parser.add_argument("--user-email", help="User email for QA seed, inspect-date, or reset.")
    args = parser.parse_args()

    snapshot_date = _parse_date(args.date)
    if args.seed_qa:
        if not args.workspace_id or not args.user_email or not args.date:
            parser.error("--seed-qa requires --workspace-id, --user-email, and --date.")
        return seed_qa_scenario_command(
            args.seed_qa,
            snapshot_date=snapshot_date,
            workspace_id=args.workspace_id,
            user_email=args.user_email,
        )
    if args.reset:
        if not args.inspect and (not args.workspace_id or not (args.user_id or args.user_email) or not args.date):
            parser.error("--reset requires --inspect SNAPSHOT_ID or --workspace-id, --user-id/--user-email, and --date.")
        return reset_snapshot(
            snapshot_id=args.inspect,
            workspace_id=args.workspace_id,
            user_id=args.user_id,
            user_email=args.user_email,
            snapshot_date=snapshot_date,
        )
    if args.cleanup_qa:
        if not args.workspace_id or not args.date:
            parser.error("--cleanup-qa requires --workspace-id and --date.")
        return cleanup_qa_data(
            snapshot_date=snapshot_date,
            workspace_id=args.workspace_id,
            user_id=args.user_id,
            user_email=args.user_email,
        )
    if args.inspect:
        return inspect_snapshot(args.inspect, as_json=args.json)
    if args.inspect_date:
        if not args.workspace_id or not (args.user_id or args.user_email) or not args.date:
            parser.error("--inspect-date requires --workspace-id, --user-id/--user-email, and --date.")
        return inspect_snapshot_by_lookup(
            workspace_id=args.workspace_id,
            user_id=args.user_id,
            user_email=args.user_email,
            snapshot_date=snapshot_date,
            as_json=args.json,
        )
    return generate_snapshots(snapshot_date)


if __name__ == "__main__":
    raise SystemExit(main())
