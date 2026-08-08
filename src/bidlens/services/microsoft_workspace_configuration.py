from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..models import ExternalIntegrationConnection, Workspace, WorkspaceIntegration
from .microsoft_integration_context import (
    CONFIGURED_STATUS,
    INDIVIDUAL_MODE,
    MICROSOFT_PROVIDER,
    resolve_microsoft_integration_context,
)


ORGANIZATION_MODE = "organization"
ALLOWED_MODES = {INDIVIDUAL_MODE, ORGANIZATION_MODE}


def normalize_microsoft_tenant_id(value: str | None) -> str:
    return str(value or "").strip().casefold()


class MicrosoftWorkspaceConfigurationError(ValueError):
    def __init__(self, code: str, message: str, *, conflict_count: int = 0) -> None:
        self.code = code
        self.conflict_count = conflict_count
        super().__init__(message)


@dataclass(frozen=True)
class MicrosoftTenantConflicts:
    authoritative_tenant_id: str
    connection_ids: tuple[int, ...]

    @property
    def count(self) -> int:
        return len(self.connection_ids)


class MicrosoftWorkspaceConfigurationService:
    """Owns workspace Microsoft mode and tenant-policy changes; never credentials."""

    def __init__(self, *, db: Session, workspace: Workspace) -> None:
        self.db = db
        self.workspace = workspace

    def integration(self) -> WorkspaceIntegration | None:
        return resolve_microsoft_integration_context(
            self.db,
            workspace=self.workspace,
        ).workspace_integration

    def _ensure_integration(self) -> WorkspaceIntegration:
        integration = self.integration()
        if integration is None:
            integration = WorkspaceIntegration(
                workspace_id=self.workspace.id,
                provider=MICROSOFT_PROVIDER,
                mode=INDIVIDUAL_MODE,
                status=CONFIGURED_STATUS,
            )
            self.db.add(integration)
            self.db.flush()
        return integration

    def distinct_member_tenant_ids(self) -> tuple[str, ...]:
        values = (
            self.db.query(ExternalIntegrationConnection.external_tenant_id)
            .filter(
                ExternalIntegrationConnection.workspace_id == self.workspace.id,
                ExternalIntegrationConnection.provider == MICROSOFT_PROVIDER,
                ExternalIntegrationConnection.external_tenant_id.isnot(None),
            )
            .distinct()
            .all()
        )
        return tuple(sorted({
            normalize_microsoft_tenant_id(value)
            for (value,) in values
            if normalize_microsoft_tenant_id(value)
        }))

    def organization_mode_conflicts(self, authoritative_tenant_id: str) -> MicrosoftTenantConflicts:
        tenant_id = normalize_microsoft_tenant_id(authoritative_tenant_id)
        if not tenant_id:
            raise MicrosoftWorkspaceConfigurationError(
                "organization_tenant_required",
                "An authoritative Microsoft tenant ID is required for organization mode.",
            )
        connections = (
            self.db.query(
                ExternalIntegrationConnection.id,
                ExternalIntegrationConnection.external_tenant_id,
            )
            .filter(
                ExternalIntegrationConnection.workspace_id == self.workspace.id,
                ExternalIntegrationConnection.provider == MICROSOFT_PROVIDER,
            )
            .order_by(ExternalIntegrationConnection.id.asc())
            .all()
        )
        ids = tuple(
            row_id
            for row_id, connection_tenant_id in connections
            if normalize_microsoft_tenant_id(connection_tenant_id) != tenant_id
        )
        return MicrosoftTenantConflicts(tenant_id, ids)

    def configure_organization_tenant(
        self,
        *,
        external_tenant_id: str,
        tenant_display_name: str | None = None,
    ) -> WorkspaceIntegration:
        tenant_id = normalize_microsoft_tenant_id(external_tenant_id)
        conflicts = self.organization_mode_conflicts(tenant_id)
        if conflicts.count:
            raise MicrosoftWorkspaceConfigurationError(
                "organization_tenant_conflict",
                f"{conflicts.count} existing Microsoft connection(s) belong to a different tenant.",
                conflict_count=conflicts.count,
            )
        integration = self._ensure_integration()
        integration.mode = ORGANIZATION_MODE
        integration.status = CONFIGURED_STATUS
        integration.external_tenant_id = tenant_id
        integration.tenant_display_name = str(tenant_display_name or "").strip() or None
        self.db.flush()
        return integration

    def set_connection_mode(
        self,
        mode: str,
        *,
        external_tenant_id: str | None = None,
        tenant_display_name: str | None = None,
    ) -> WorkspaceIntegration:
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode not in ALLOWED_MODES:
            raise MicrosoftWorkspaceConfigurationError("invalid_connection_mode", "Invalid Microsoft connection mode.")
        if normalized_mode == ORGANIZATION_MODE:
            # Never infer a tenant from the first or only connected member.
            return self.configure_organization_tenant(
                external_tenant_id=str(external_tenant_id or ""),
                tenant_display_name=tenant_display_name,
            )

        integration = self._ensure_integration()
        integration.mode = INDIVIDUAL_MODE
        integration.status = CONFIGURED_STATUS
        integration.external_tenant_id = None
        integration.tenant_display_name = None
        self.db.flush()
        return integration

    def validate_member_tenant(self, external_tenant_id: str | None) -> None:
        integration = self.integration()
        if integration is None or integration.mode == INDIVIDUAL_MODE:
            return
        authoritative_tenant_id = normalize_microsoft_tenant_id(integration.external_tenant_id)
        if not authoritative_tenant_id:
            raise MicrosoftWorkspaceConfigurationError(
                "organization_tenant_not_configured",
                "The workspace Microsoft tenant is not configured.",
            )
        if normalize_microsoft_tenant_id(external_tenant_id) != authoritative_tenant_id:
            raise MicrosoftWorkspaceConfigurationError(
                "organization_tenant_mismatch",
                "The connected Microsoft identity does not belong to this workspace tenant.",
            )
