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
from bidlens.models import DailySnapshot, Organization, OrganizationMembership, User, Workspace
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
        return f"{actor} · {_opportunity_title(item)} · {item.get('from') or 'None'} to {item.get('to') or 'None'}"

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
    parser.add_argument("--json", action="store_true", help="Print raw snapshot JSON when using --inspect.")
    args = parser.parse_args()

    if args.inspect:
        return inspect_snapshot(args.inspect, as_json=args.json)
    return generate_snapshots(_parse_date(args.date))


if __name__ == "__main__":
    raise SystemExit(main())
