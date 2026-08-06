"""Deterministic authoritative current-state assembly for GUTS."""

from __future__ import annotations

from sqlalchemy.orm import Session, joinedload

from ...models import Opportunity, OpportunityOutcome, User, Vote, Workspace
from ..opportunity_descriptions import select_opportunity_description
from ..account_aliases import resolve_account_display_name
from .contracts import (
    CurrentOpportunityState,
    CurrentStateField,
    InterestedTeammate,
    OrganizationOutcomeState,
    SalesforceLinkState,
    current_state_source_id,
)


CURRENT_DESCRIPTION_MAX_CHARACTERS = 12_000


class CurrentStateScopeError(ValueError):
    pass


def _date_value(value) -> str | None:
    return value.isoformat() if value is not None else None


def _field(opportunity_id: int, field_name: str, value) -> CurrentStateField:
    return CurrentStateField(
        value=value,
        source_id=current_state_source_id(opportunity_id, field_name),
    )


class CurrentStateAssembler:
    def __init__(self, db: Session, *, description_max_characters: int = CURRENT_DESCRIPTION_MAX_CHARACTERS):
        if description_max_characters <= 0:
            raise ValueError("description_max_characters must be positive")
        self.db = db
        self.description_max_characters = description_max_characters

    def build(
        self,
        *,
        opportunity: Opportunity,
        organization_id: int,
        workspace_id: int,
    ) -> CurrentOpportunityState:
        workspace = self.db.get(Workspace, workspace_id)
        if opportunity.organization_id != organization_id:
            raise CurrentStateScopeError("Opportunity is outside the requested organization scope.")
        if workspace is None or workspace.organization_id != organization_id:
            raise CurrentStateScopeError("Workspace is outside the requested organization scope.")

        description = select_opportunity_description(opportunity)
        original_description_count = len(description)
        description_was_truncated = original_description_count > self.description_max_characters
        if description_was_truncated:
            description = description[: self.description_max_characters - 1].rstrip() + "…"

        outcome_row = self.db.query(OpportunityOutcome).options(
            joinedload(OpportunityOutcome.recorded_by_user)
        ).filter(
            OpportunityOutcome.organization_id == organization_id,
            OpportunityOutcome.opportunity_id == opportunity.id,
        ).first()
        outcome = None
        if outcome_row is not None:
            recorder = outcome_row.recorded_by_user
            display_name = ((recorder.name or recorder.email) if recorder else "Unknown user").strip()
            outcome = OrganizationOutcomeState(
                outcome_type=outcome_row.outcome_type,
                recorded_at=outcome_row.recorded_at,
                recorded_by_user_id=outcome_row.recorded_by,
                recorded_by_display_name=display_name,
            )

        vote_rows = self.db.query(Vote.user_id, User.name, User.email).join(
            User, User.id == Vote.user_id
        ).filter(
            Vote.org_id == organization_id,
            Vote.opp_id == opportunity.id,
            Vote.vote == "PURSUE",
        ).all()
        teammates = tuple(sorted(
            (
                InterestedTeammate(
                    user_id=user_id,
                    display_name=(name or email or f"User {user_id}").strip(),
                )
                for user_id, name, email in vote_rows
            ),
            key=lambda item: (item.display_name.casefold(), item.user_id),
        ))

        linked_to_salesforce = bool(
            opportunity.salesforce_opportunity_id or opportunity.salesforce_opportunity_url
        )
        salesforce = SalesforceLinkState(
            linked=linked_to_salesforce,
            url=opportunity.salesforce_opportunity_url if linked_to_salesforce else None,
        )

        return CurrentOpportunityState(
            opportunity_id=opportunity.id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            title=_field(opportunity.id, "title", opportunity.title),
            client=_field(
                opportunity.id,
                "client",
                resolve_account_display_name(opportunity.agency),
            ),
            description=_field(opportunity.id, "description", description or None),
            response_deadline=_field(opportunity.id, "response_deadline", _date_value(opportunity.response_deadline)),
            posted_date=_field(opportunity.id, "posted_date", _date_value(opportunity.posted_date)),
            solicitation_number=_field(opportunity.id, "solicitation_number", opportunity.solicitation_number),
            opportunity_type=_field(opportunity.id, "opportunity_type", opportunity.opportunity_type),
            source_stage=_field(opportunity.id, "source_stage", opportunity.source_stage),
            source=_field(opportunity.id, "source", opportunity.source),
            source_record_id=_field(opportunity.id, "source_record_id", opportunity.source_record_id),
            source_url=_field(opportunity.id, "source_url", opportunity.source_url),
            sam_url=_field(opportunity.id, "sam_url", opportunity.sam_url),
            bidlens_id=_field(opportunity.id, "bidlens_id", str(opportunity.bidlens_id) if opportunity.bidlens_id else None),
            sam_notice_id=_field(opportunity.id, "sam_notice_id", opportunity.sam_notice_id),
            naics=_field(opportunity.id, "naics", opportunity.naics),
            naics_title=_field(opportunity.id, "naics_title", opportunity.naics_title),
            set_aside=_field(opportunity.id, "set_aside", opportunity.set_aside),
            description_original_character_count=original_description_count,
            description_was_truncated=description_was_truncated,
            outcome=outcome,
            interested_teammates=teammates,
            salesforce=salesforce,
        )

    @staticmethod
    def compact_snapshot(state: CurrentOpportunityState) -> dict:
        field_names = (
            "title", "client", "description", "response_deadline", "posted_date", "solicitation_number",
            "opportunity_type", "source_stage", "source", "source_record_id", "source_url", "sam_url",
            "bidlens_id", "sam_notice_id", "naics", "naics_title", "set_aside",
        )
        snapshot = {
            "opportunity_id": state.opportunity_id,
            "organization_id": state.organization_id,
            "workspace_id": state.workspace_id,
            "fields": {name: getattr(state, name).value for name in field_names},
            "description_original_character_count": state.description_original_character_count,
            "description_was_truncated": state.description_was_truncated,
            "outcome": state.outcome.serializable_dict() if state.outcome else None,
            "interested_teammates": [item.serializable_dict() for item in state.interested_teammates],
            "salesforce": state.salesforce.serializable_dict(),
        }
        return snapshot
