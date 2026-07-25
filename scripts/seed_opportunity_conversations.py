#!/usr/bin/env python3
"""Seed local-only opportunity conversation fixtures.

This creates development conversation/timeline rows for one existing
opportunity. It is intentionally conservative: it only runs against local
SQLite databases and refuses production-looking environments.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from bidlens.config import DATABASE_URL  # noqa: E402
from bidlens.database import SessionLocal  # noqa: E402
from bidlens.models import (  # noqa: E402
    Opportunity,
    OpportunityActivityEvent,
    OpportunityConversation,
    OrganizationMembership,
    User,
    Workspace,
)
from bidlens.services.opportunity_conversations import (  # noqa: E402
    EVENT_TYPE_CONVERSATION_MESSAGE,
    EVENT_TYPE_CONVERSATION_STARTED,
    EVENT_TYPE_STATUS_SUMMARY_UPDATED,
    create_activity_for_authorized_opportunity,
    create_conversation_for_authorized_opportunity,
)


SEED_PROVIDER = "seeded"
SEED_EXTERNAL_PREFIX = "bidlens-dev-conversation"
SEED_USER_EMAILS = (
    ("jane.seed@bidlens.dev", "Jane Seed"),
    ("john.seed@bidlens.dev", "John Seed"),
    ("sarah.seed@bidlens.dev", "Sarah Seed"),
)


def _env_looks_production() -> bool:
    values = [
        os.getenv("ENV"),
        os.getenv("APP_ENV"),
        os.getenv("BIDLENS_ENV"),
        os.getenv("FASTAPI_ENV"),
    ]
    return any(str(value or "").strip().lower() in {"prod", "production"} for value in values)


def _sqlite_path_from_url(database_url: str) -> Path | None:
    parsed = urlparse(database_url)
    if parsed.scheme != "sqlite":
        return None
    if database_url.startswith("sqlite:///:memory:"):
        return REPO_ROOT / ":memory:"
    if database_url.startswith("sqlite:///"):
        raw_path = database_url.removeprefix("sqlite:///")
        path = Path(raw_path)
        if not path.is_absolute():
            path = (REPO_ROOT / path).resolve()
        return path
    return None


def assert_safe_local_seed_environment() -> None:
    if _env_looks_production():
        raise SystemExit("Refusing to seed opportunity conversations: environment looks like production.")

    db_path = _sqlite_path_from_url(DATABASE_URL)
    if db_path is None:
        raise SystemExit(
            "Refusing to seed opportunity conversations: this utility only supports local SQLite databases. "
            f"DATABASE_URL={DATABASE_URL!r}"
        )
    if db_path.name != ":memory:":
        try:
            db_path.relative_to(REPO_ROOT)
        except ValueError as exc:
            raise SystemExit(
                "Refusing to seed opportunity conversations: SQLite database is outside the repository."
            ) from exc


def _seed_users(db, workspace: Workspace) -> list[User]:
    users: list[User] = []
    for email, name in SEED_USER_EMAILS:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            user = User(email=email, name=name, organization_id=workspace.organization_id)
            db.add(user)
            db.flush()
        membership = (
            db.query(OrganizationMembership)
            .filter(
                OrganizationMembership.organization_id == workspace.organization_id,
                OrganizationMembership.user_id == user.id,
            )
            .first()
        )
        if not membership:
            db.add(
                OrganizationMembership(
                    organization_id=workspace.organization_id,
                    user_id=user.id,
                    role="member",
                )
            )
        users.append(user)
    return users


def _select_workspace(db, workspace_id: int | None) -> Workspace:
    query = db.query(Workspace)
    if workspace_id is not None:
        query = query.filter(Workspace.id == workspace_id)
    workspace = query.order_by(Workspace.id.asc()).first()
    if not workspace:
        raise SystemExit("No workspace found. Create a local development workspace first.")
    return workspace


def _select_opportunity(db, workspace: Workspace, opportunity_id: int | None) -> Opportunity:
    query = db.query(Opportunity).filter(Opportunity.organization_id == workspace.organization_id)
    if opportunity_id is not None:
        query = query.filter(Opportunity.id == opportunity_id)
    opportunity = query.order_by(Opportunity.updated_at.desc(), Opportunity.id.asc()).first()
    if not opportunity:
        raise SystemExit("No opportunity found for the selected workspace.")
    return opportunity


def cleanup_seeded_conversations(db, *, workspace_id: int, opportunity_id: int) -> int:
    conversations = (
        db.query(OpportunityConversation)
        .filter(
            OpportunityConversation.workspace_id == workspace_id,
            OpportunityConversation.opportunity_id == opportunity_id,
            OpportunityConversation.provider == SEED_PROVIDER,
            OpportunityConversation.external_conversation_id.like(f"{SEED_EXTERNAL_PREFIX}-%"),
        )
        .all()
    )
    conversation_ids = [conversation.id for conversation in conversations]
    deleted_events = 0
    if conversation_ids:
        deleted_events = (
            db.query(OpportunityActivityEvent)
            .filter(OpportunityActivityEvent.conversation_id.in_(conversation_ids))
            .delete(synchronize_session=False)
        )
    seeded_events = (
        db.query(OpportunityActivityEvent)
        .filter(
            OpportunityActivityEvent.workspace_id == workspace_id,
            OpportunityActivityEvent.opportunity_id == opportunity_id,
        )
        .all()
    )
    for event in seeded_events:
        metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
        if metadata.get("seed_key") == "opportunity-conversations-dev":
            db.delete(event)
            deleted_events += 1
    deleted_conversations = len(conversations)
    for conversation in conversations:
        db.delete(conversation)
    db.flush()
    return deleted_conversations + deleted_events


def seed_conversations(db, *, workspace_id: int | None = None, opportunity_id: int | None = None, cleanup: bool = False) -> tuple[int, int, Opportunity]:
    assert_safe_local_seed_environment()
    workspace = _select_workspace(db, workspace_id)
    opportunity = _select_opportunity(db, workspace, opportunity_id)
    cleanup_seeded_conversations(db, workspace_id=workspace.id, opportunity_id=opportunity.id)
    if cleanup:
        db.commit()
        return 0, 0, opportunity

    jane, john, sarah = _seed_users(db, workspace)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    conversations = [
        create_conversation_for_authorized_opportunity(
            db,
            opportunity=opportunity,
            provider=SEED_PROVIDER,
            external_conversation_id=f"{SEED_EXTERNAL_PREFIX}-fit",
            subject=f"Capture discussion: {opportunity.title[:80]}",
            started_by_user_id=jane.id,
            participant_summary="Jane Seed, Sarah Seed",
            message_count=3,
            first_message_at=now - timedelta(hours=6),
            last_message_at=now - timedelta(hours=2),
        ),
        create_conversation_for_authorized_opportunity(
            db,
            opportunity=opportunity,
            provider=SEED_PROVIDER,
            external_conversation_id=f"{SEED_EXTERNAL_PREFIX}-questions",
            subject="Questions for qualification review",
            started_by_user_id=john.id,
            participant_summary="John Seed, Jane Seed",
            message_count=2,
            first_message_at=now - timedelta(hours=5),
            last_message_at=now - timedelta(hours=1, minutes=15),
        ),
    ]
    db.flush()
    events = [
        create_activity_for_authorized_opportunity(
            db,
            opportunity=opportunity,
            conversation=conversations[0],
            actor_user_id=jane.id,
            event_type=EVENT_TYPE_CONVERSATION_STARTED,
            title="Jane Seed started a capture discussion.",
            description="Placeholder conversation activity for local development review.",
            metadata_json={"seed_key": "opportunity-conversations-dev"},
            occurred_at=now - timedelta(hours=6),
        ),
        create_activity_for_authorized_opportunity(
            db,
            opportunity=opportunity,
            conversation=conversations[0],
            actor_user_id=sarah.id,
            event_type=EVENT_TYPE_CONVERSATION_MESSAGE,
            title="Sarah Seed added a qualification note.",
            description="Message bodies are intentionally not stored in this V1 foundation.",
            metadata_json={"seed_key": "opportunity-conversations-dev"},
            occurred_at=now - timedelta(hours=2),
        ),
        create_activity_for_authorized_opportunity(
            db,
            opportunity=opportunity,
            conversation=conversations[1],
            actor_user_id=john.id,
            event_type=EVENT_TYPE_CONVERSATION_STARTED,
            title="John Seed opened qualification questions.",
            description="Second seeded conversation to validate multiple conversation rendering.",
            metadata_json={"seed_key": "opportunity-conversations-dev"},
            occurred_at=now - timedelta(hours=5),
        ),
        create_activity_for_authorized_opportunity(
            db,
            opportunity=opportunity,
            actor_user_id=None,
            event_type=EVENT_TYPE_STATUS_SUMMARY_UPDATED,
            title="Current status placeholder refreshed.",
            description="This timestamp is seeded for UI review only; no AI summary was generated.",
            metadata_json={"seed_key": "opportunity-conversations-dev", "placeholder": True},
            occurred_at=now - timedelta(minutes=45),
        ),
    ]
    db.commit()
    return len(conversations), len(events), opportunity


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed local-only opportunity conversation fixtures.")
    parser.add_argument("--workspace-id", type=int, default=None)
    parser.add_argument("--opportunity-id", type=int, default=None)
    parser.add_argument("--cleanup", action="store_true", help="Remove seeded conversation fixtures for the selected opportunity.")
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        conversation_count, event_count, opportunity = seed_conversations(
            db,
            workspace_id=args.workspace_id,
            opportunity_id=args.opportunity_id,
            cleanup=args.cleanup,
        )
        if args.cleanup:
            print(f"Removed seeded conversation fixtures for opportunity {opportunity.id}.")
        else:
            print(
                "Seeded "
                f"{conversation_count} conversations and {event_count} activity events "
                f"for opportunity {opportunity.id}."
            )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
