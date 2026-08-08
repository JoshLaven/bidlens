from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..models import ExternalIntegrationConnection, User, Workspace, WorkspaceIntegration


MICROSOFT_PROVIDER = "microsoft"
INDIVIDUAL_MODE = "individual"
NOT_CONFIGURED_STATUS = "not_configured"
CONFIGURED_STATUS = "configured"


@dataclass(frozen=True)
class MicrosoftIntegrationContext:
    """Read-only workspace and current-member Microsoft integration state."""

    workspace_integration: WorkspaceIntegration | None
    current_user_connection: ExternalIntegrationConnection | None
    workspace_id: int
    provider: str = MICROSOFT_PROVIDER

    @property
    def mode(self) -> str:
        # A missing parent is a rolling-deployment/legacy compatibility state.
        return self.workspace_integration.mode if self.workspace_integration else INDIVIDUAL_MODE

    @property
    def status(self) -> str:
        if self.workspace_integration:
            return self.workspace_integration.status
        return CONFIGURED_STATUS if self.current_user_connection else NOT_CONFIGURED_STATUS

    @property
    def is_configured(self) -> bool:
        return self.status == CONFIGURED_STATUS

    @property
    def is_individual_mode(self) -> bool:
        return self.mode == INDIVIDUAL_MODE

    @property
    def is_organization_mode(self) -> bool:
        return self.mode == "organization"

    @property
    def external_tenant_id(self) -> str | None:
        return self.workspace_integration.external_tenant_id if self.workspace_integration else None

    @property
    def tenant_display_name(self) -> str | None:
        return self.workspace_integration.tenant_display_name if self.workspace_integration else None

    @property
    def has_current_user_connection(self) -> bool:
        return self.current_user_connection is not None


def resolve_microsoft_integration_context(
    db: Session,
    *,
    workspace: Workspace,
    current_user: User | None = None,
    current_user_id: int | None = None,
) -> MicrosoftIntegrationContext:
    """Resolve Microsoft state without selecting another member's credential."""
    if current_user is not None:
        if current_user_id is not None and current_user_id != current_user.id:
            raise ValueError("current_user and current_user_id must identify the same user")
        current_user_id = current_user.id

    workspace_integration = (
        db.query(WorkspaceIntegration)
        .filter(
            WorkspaceIntegration.workspace_id == workspace.id,
            WorkspaceIntegration.provider == MICROSOFT_PROVIDER,
        )
        .one_or_none()
    )
    current_user_connection = None
    if current_user_id is not None:
        current_user_connection = (
            db.query(ExternalIntegrationConnection)
            .filter(
                ExternalIntegrationConnection.workspace_id == workspace.id,
                ExternalIntegrationConnection.user_id == current_user_id,
                ExternalIntegrationConnection.provider == MICROSOFT_PROVIDER,
            )
            .one_or_none()
        )

    return MicrosoftIntegrationContext(
        workspace_integration=workspace_integration,
        current_user_connection=current_user_connection,
        workspace_id=workspace.id,
    )
