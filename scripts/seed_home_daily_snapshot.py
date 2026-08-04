"""Seed one representative Home Daily Snapshot in local SQLite only.

This is a development/QA helper. It writes only the selected DailySnapshot row
and never creates opportunities, events, votes, memberships, or users.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import Callable, TextIO


ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from bidlens.database import SessionLocal
from bidlens.models import DailySnapshot, OrganizationMembership, User, Workspace
from bidlens.services.daily_snapshot import SNAPSHOT_VERSION


QA_FIXTURE_KEY = "home_daily_snapshot_qa_v1"


def _require_sqlite(db) -> None:
    backend = str(db.get_bind().dialect.name or "").lower()
    if backend != "sqlite":
        raise RuntimeError(
            "Refusing to modify Daily Snapshot data: this development helper requires SQLite."
        )


def _selected_workspace_user(db, *, workspace_id: int, user_id: int) -> tuple[Workspace, User]:
    workspace = db.query(Workspace).filter(Workspace.id == workspace_id).one_or_none()
    if workspace is None:
        raise ValueError(f"Workspace {workspace_id} was not found.")
    user = db.query(User).filter(User.id == user_id).one_or_none()
    if user is None:
        raise ValueError(f"User {user_id} was not found.")
    membership = (
        db.query(OrganizationMembership.id)
        .filter(
            OrganizationMembership.organization_id == workspace.organization_id,
            OrganizationMembership.user_id == user.id,
        )
        .one_or_none()
    )
    if membership is None:
        raise ValueError(f"User {user_id} is not a member of workspace {workspace_id}.")
    return workspace, user


def _representative_payload(
    *, workspace: Workspace, user: User, snapshot_date: dt.date,
) -> dict[str, object]:
    activity_date = snapshot_date - dt.timedelta(days=1)
    occurred_at = dt.datetime.combine(activity_date, dt.time(hour=15)).isoformat()
    return {
        "version": SNAPSHOT_VERSION,
        "qa_fixture": QA_FIXTURE_KEY,
        "snapshot_date": snapshot_date.isoformat(),
        "activity_date": activity_date.isoformat(),
        "activity_window": {
            "start": dt.datetime.combine(activity_date, dt.time.min).isoformat(),
            "end": dt.datetime.combine(snapshot_date, dt.time.min).isoformat(),
            "basis": "calendar_day",
        },
        "workspace": {"id": workspace.id, "name": workspace.name},
        "user": {"id": user.id, "name": user.name, "email": user.email},
        "summary": {
            "new_feed_count": 0,
            "shortlist_update_count": 1,
            "team_signal_count": 1,
            "shortlist_deadline_count": 1,
            "connector_issue_count": 0,
        },
        "shortlist_deadlines": [{
            "title": "Representative Shortlisted Opportunity",
            "subtitle": "Due tomorrow",
            "destination_url": "/my-shortlist",
        }],
        "shortlist_updates": [{
            "title": "Representative Shortlisted Opportunity",
            "subtitle": "Response deadline changed from Aug 18 to Aug 25",
            "destination_url": "/my-shortlist",
            "detected_at": occurred_at,
        }],
        "team_signals": [{
            "title": "Representative Shortlisted Opportunity",
            "subtitle": "Kendall Roy showed interest",
            "destination_url": "/my-shortlist",
            "occurred_at": occurred_at,
        }],
        "connector_issues": [],
        "my_shortlist": [],
        "my_lanes": [],
        "my_lane_context": [],
        "new_feed_opportunities": [],
        "new_opportunities": [],
        "updated_opportunities": [],
        "upcoming_deadlines": [],
        "interested_activity": [],
        "shortlist_changes": [],
    }


def seed_home_snapshot(
    db, *, workspace_id: int, user_id: int, snapshot_date: dt.date,
) -> tuple[DailySnapshot, str]:
    """Create or replace exactly one selected user's local QA snapshot."""
    _require_sqlite(db)
    workspace, user = _selected_workspace_user(
        db, workspace_id=workspace_id, user_id=user_id,
    )
    snapshot = (
        db.query(DailySnapshot)
        .filter(
            DailySnapshot.workspace_id == workspace.id,
            DailySnapshot.user_id == user.id,
            DailySnapshot.snapshot_date == snapshot_date,
        )
        .one_or_none()
    )
    action = "updated" if snapshot is not None else "created"
    if snapshot is None:
        snapshot = DailySnapshot(
            workspace_id=workspace.id,
            user_id=user.id,
            snapshot_date=snapshot_date,
        )
        db.add(snapshot)
    snapshot.status = "completed"
    snapshot.snapshot_json = _representative_payload(
        workspace=workspace, user=user, snapshot_date=snapshot_date,
    )
    db.commit()
    db.refresh(snapshot)
    return snapshot, action


def reset_home_snapshot(
    db, *, workspace_id: int, user_id: int, snapshot_date: dt.date,
) -> bool:
    """Delete the selected snapshot only when it was created by this helper."""
    _require_sqlite(db)
    snapshot = (
        db.query(DailySnapshot)
        .filter(
            DailySnapshot.workspace_id == workspace_id,
            DailySnapshot.user_id == user_id,
            DailySnapshot.snapshot_date == snapshot_date,
        )
        .one_or_none()
    )
    if snapshot is None:
        return False
    payload = snapshot.snapshot_json if isinstance(snapshot.snapshot_json, dict) else {}
    if payload.get("qa_fixture") != QA_FIXTURE_KEY:
        raise RuntimeError(
            "Refusing to reset a Daily Snapshot not created by this QA helper."
        )
    db.delete(snapshot)
    db.commit()
    return True


def run(
    *, workspace_id: int, user_id: int, snapshot_date: dt.date,
    reset: bool = False, session_factory: Callable = SessionLocal,
    output: TextIO = sys.stdout,
) -> int:
    db = session_factory()
    try:
        if reset:
            deleted = reset_home_snapshot(
                db, workspace_id=workspace_id, user_id=user_id,
                snapshot_date=snapshot_date,
            )
            print("Daily Snapshot QA fixture reset" if deleted else "No matching QA fixture found", file=output)
            return 0
        snapshot, action = seed_home_snapshot(
            db, workspace_id=workspace_id, user_id=user_id,
            snapshot_date=snapshot_date,
        )
        print("Home Daily Snapshot QA fixture (SQLite only)", file=output)
        print(f"action={action}", file=output)
        print(f"snapshot_id={snapshot.id}", file=output)
        print(f"workspace_id={snapshot.workspace_id}", file=output)
        print(f"user_id={snapshot.user_id}", file=output)
        print(f"snapshot_date={snapshot.snapshot_date.isoformat()}", file=output)
        return 0
    except Exception as exc:
        db.rollback()
        print(f"Daily Snapshot QA helper failed: {exc}", file=output)
        return 1
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Development/QA only: seed one representative Home snapshot in SQLite.",
    )
    parser.add_argument("--workspace-id", type=int, required=True)
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--date", required=True, help="Snapshot date in YYYY-MM-DD format.")
    parser.add_argument(
        "--reset", action="store_true",
        help="Delete the selected snapshot if it was created by this helper.",
    )
    args = parser.parse_args(argv)
    try:
        snapshot_date = dt.date.fromisoformat(args.date)
    except ValueError:
        parser.error("--date must use YYYY-MM-DD format.")
    return run(
        workspace_id=args.workspace_id,
        user_id=args.user_id,
        snapshot_date=snapshot_date,
        reset=args.reset,
    )


if __name__ == "__main__":
    raise SystemExit(main())
