from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from time import monotonic
from typing import Callable

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import ExternalIntegrationConnection, Organization, Workspace
from .microsoft import PROVIDER_MICROSOFT, STATUS_CONNECTED
from .microsoft_conversation_sync import (
    eligible_tracked_microsoft_conversations,
    sync_tracked_microsoft_conversations,
)


@dataclass
class OutlookSyncJobResult:
    workspaces_considered: int = 0
    workspaces_synced: int = 0
    workspaces_skipped: int = 0
    workspaces_failed: int = 0
    conversations_checked: int = 0
    conversations_succeeded: int = 0
    conversations_failed: int = 0
    messages_imported: int = 0
    duplicates_skipped: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def _log(message: str) -> None:
    print(message, flush=True)


def _candidate_workspace_ids(db: Session) -> list[int]:
    tracked = eligible_tracked_microsoft_conversations(db, workspace_id=Workspace.id).exists()
    return [
        int(row[0])
        for row in (
            db.query(Workspace.id)
            .join(Organization, Organization.id == Workspace.organization_id)
            .filter(Organization.is_live.is_(True), tracked)
            .order_by(Workspace.id.asc())
            .all()
        )
    ]


def _has_usable_connection(db: Session, workspace_id: int) -> bool:
    return db.query(ExternalIntegrationConnection.id).filter(
        ExternalIntegrationConnection.workspace_id == workspace_id,
        ExternalIntegrationConnection.provider == PROVIDER_MICROSOFT,
        ExternalIntegrationConnection.connection_status == STATUS_CONNECTED,
        ExternalIntegrationConnection.external_user_id.isnot(None),
        ExternalIntegrationConnection.connected_email.isnot(None),
        or_(
            ExternalIntegrationConnection.encrypted_access_token.isnot(None),
            ExternalIntegrationConnection.encrypted_refresh_token.isnot(None),
        ),
    ).first() is not None


def run_outlook_conversation_sync_job(
    *,
    session_factory: Callable[[], Session] = SessionLocal,
) -> dict[str, int]:
    """Run Phase 2A synchronization for eligible live workspaces."""
    started = monotonic()
    result = OutlookSyncJobResult()
    _log(f"Outlook conversation sync started at {datetime.now(timezone.utc).isoformat()}")
    list_db = session_factory()
    try:
        workspace_ids = _candidate_workspace_ids(list_db)
    finally:
        list_db.close()

    for workspace_id in workspace_ids:
        result.workspaces_considered += 1
        db = session_factory()
        workspace_started = monotonic()
        try:
            workspace = db.query(Workspace).filter(Workspace.id == workspace_id).one_or_none()
            if workspace is None or not _has_usable_connection(db, workspace_id):
                result.workspaces_skipped += 1
                _log(f"Outlook sync workspace_id={workspace_id} outcome=skipped reason=ineligible")
                continue
            counts = sync_tracked_microsoft_conversations(
                db,
                workspace=workspace,
                stop_on_authorization_failure=True,
            )
            result.workspaces_synced += 1
            result.conversations_checked += counts["conversations_checked"]
            result.conversations_succeeded += counts["conversations_succeeded"]
            result.conversations_failed += counts["conversations_failed"]
            result.messages_imported += counts["new_messages_imported"]
            result.duplicates_skipped += counts["duplicates_skipped"]
            duration_ms = int((monotonic() - workspace_started) * 1000)
            _log(
                f"Outlook sync workspace_id={workspace_id} outcome=completed "
                f"conversations_checked={counts['conversations_checked']} "
                f"messages_imported={counts['new_messages_imported']} "
                f"duplicates_skipped={counts['duplicates_skipped']} "
                f"errors={counts['conversations_failed']} duration_ms={duration_ms}"
            )
        except Exception as exc:
            db.rollback()
            result.workspaces_failed += 1
            # Exception messages can contain provider data; log only the safe class name.
            _log(f"Outlook sync workspace_id={workspace_id} outcome=failed error_type={type(exc).__name__}")
        finally:
            db.close()

    aggregate = result.to_dict()
    fields = " ".join(f"{key}={value}" for key, value in aggregate.items())
    _log(f"Outlook conversation sync finished {fields} duration_ms={int((monotonic() - started) * 1000)}")
    return aggregate
