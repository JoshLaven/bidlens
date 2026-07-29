from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...models import (
    IngestionRun,
    IngestionRunDetail,
    Opportunity,
    OpportunityIntakeDraft,
    OrganizationMembership,
    User,
    Vote,
    Workspace,
)
from ..opportunity_history import record_imported_history
from ..pursuit_lanes import refresh_opportunity_lane_matches
from ..shortlisting import ensure_user_shortlisted
from .contracts import (
    INTAKE_DECISION_STATE,
    INTAKE_QUALIFICATION_STATUS,
    INTAKE_SOURCE,
    IntakeCandidate,
    OpportunityPublishResult,
)
from .drafts import preserve_materials_for_opportunity
from .duplicates import DuplicateCheckResult, find_publication_duplicates
from .normalization import opportunity_field_values
from .validation import ValidationError, validate_candidate


class OpportunityPublicationError(ValueError):
    pass


class OpportunityPublicationAccessError(OpportunityPublicationError):
    pass


class OpportunityPublicationConflict(OpportunityPublicationError):
    pass


class OpportunityPublicationValidationError(OpportunityPublicationError):
    def __init__(self, errors: tuple[ValidationError, ...]):
        super().__init__("Reviewed opportunity values are invalid")
        self.errors = errors


class OpportunityDuplicateError(OpportunityPublicationConflict):
    def __init__(self, duplicates: DuplicateCheckResult):
        super().__init__("An exact duplicate opportunity already exists")
        self.duplicates = duplicates


def _result_for(
    db: Session,
    draft: OpportunityIntakeDraft,
    opportunity: Opportunity,
) -> OpportunityPublishResult:
    vote_exists = db.query(Vote.id).filter(
        Vote.org_id == opportunity.organization_id,
        Vote.opp_id == opportunity.id,
        Vote.user_id == draft.created_by_user_id,
        Vote.vote == "PURSUE",
    ).first() is not None
    return OpportunityPublishResult(
        opportunity_id=opportunity.id,
        source_record_id=opportunity.source_record_id,
        solicitation_number=opportunity.solicitation_number or draft.internal_reference,
        added_to_shortlist=vote_exists,
        qualification_status=opportunity.qualification_status,
        decision_state=opportunity.decision_state,
        metadata={"idempotent_replay": True, "probable_duplicates": []},
    )


def _require_publish_access(db: Session, *, draft: OpportunityIntakeDraft, user: User) -> None:
    workspace = db.query(Workspace.id).filter(
        Workspace.id == draft.workspace_id,
        Workspace.organization_id == draft.organization_id,
    ).first()
    membership = db.query(OrganizationMembership.id).filter(
        OrganizationMembership.organization_id == draft.organization_id,
        OrganizationMembership.user_id == user.id,
    ).first()
    if (
        workspace is None
        or membership is None
        or draft.created_by_user_id != user.id
    ):
        raise OpportunityPublicationAccessError("Opportunity intake draft is not available")


def _record_ingestion_audit(
    db: Session,
    *,
    draft: OpportunityIntakeDraft,
    opportunity: Opportunity,
    user: User,
    now: datetime,
    material_count: int,
) -> None:
    run = IngestionRun(
        source=INTAKE_SOURCE,
        organization_id=draft.organization_id,
        user_id=user.id,
        started_at=now,
        finished_at=now,
        status="completed",
        processed_count=1,
        created_count=1,
        inserted_count=1,
        reason_summary_json={
            "intake_method": draft.intake_method,
            "intake_draft_id": draft.id,
            "source_material_count": material_count,
        },
        notes="Published reviewed opportunity intake draft",
    )
    db.add(run)
    db.flush()
    db.add(IngestionRunDetail(
        ingestion_run_id=run.id,
        source=INTAKE_SOURCE,
        source_record_id=opportunity.source_record_id,
        title=opportunity.title,
        result="created",
        reason="Reviewed user intake published directly to Feed eligibility",
        matched_opportunity_id=opportunity.id,
        changed_fields_json={"intake_draft_id": draft.id},
        processed_at=now,
    ))


class OpportunityPublisher:
    @staticmethod
    def publish_reviewed_draft(
        db: Session,
        *,
        draft_id: int,
        publishing_user: User,
        reviewed_candidate: IntakeCandidate | Mapping[str, Any],
        add_to_shortlist: bool,
        idempotency_key: str,
        saved_on: date | None = None,
        now: datetime | None = None,
    ) -> OpportunityPublishResult:
        """Publish a reviewed draft with one service-owned database commit."""
        key = str(idempotency_key or "").strip()
        if not key:
            raise OpportunityPublicationValidationError((
                ValidationError("idempotency_key", "required", "Publish idempotency key is required."),
            ))
        current_time = now or datetime.utcnow()
        try:
            draft = (
                db.query(OpportunityIntakeDraft)
                .filter(OpportunityIntakeDraft.id == draft_id)
                .with_for_update()
                .one_or_none()
            )
            if draft is None:
                raise OpportunityPublicationAccessError("Opportunity intake draft is not available")
            _require_publish_access(db, draft=draft, user=publishing_user)

            if db.get_bind().dialect.name == "postgresql":
                db.execute(
                    text("SELECT pg_advisory_xact_lock(:namespace, :organization_id)"),
                    {"namespace": 1179209541, "organization_id": draft.organization_id},
                )

            if draft.published_opportunity_id is not None:
                if draft.publish_idempotency_key == key:
                    result = _result_for(db, draft, draft.published_opportunity)
                    db.commit()
                    return result
                raise OpportunityPublicationConflict("Opportunity intake draft is already published")
            if draft.publish_idempotency_key and draft.publish_idempotency_key != key:
                raise OpportunityPublicationConflict("Opportunity intake draft has a different publish key")

            validation = validate_candidate(reviewed_candidate)
            if not validation.is_valid:
                raise OpportunityPublicationValidationError(validation.errors)
            candidate = validation.candidate
            duplicates = find_publication_duplicates(db, draft=draft, candidate=candidate)
            if duplicates.has_exact_match:
                raise OpportunityDuplicateError(duplicates)

            if not draft.internal_reference:
                raise OpportunityPublicationConflict("Opportunity intake draft has no persisted reference")
            solicitation_number = candidate.solicitation_number or draft.internal_reference
            values = opportunity_field_values(
                candidate,
                saved_on=saved_on or current_time.date(),
                source_record_id=draft.internal_reference,
                solicitation_number=solicitation_number,
            )
            opportunity = Opportunity(
                organization_id=draft.organization_id,
                qualification_status=INTAKE_QUALIFICATION_STATUS,
                decision_state=INTAKE_DECISION_STATE,
                raw_source_payload={
                    "intake_draft_id": draft.id,
                    "intake_method": draft.intake_method,
                    "workspace_id": draft.workspace_id,
                },
                created_at=current_time,
                **values,
            )
            db.add(opportunity)
            db.flush()

            material_count = preserve_materials_for_opportunity(
                db, draft=draft, opportunity=opportunity
            )
            draft.candidate_fields_json = {
                key: value.isoformat() if isinstance(value, date) else value
                for key, value in asdict(candidate).items()
                if value is not None
            }
            draft.add_to_shortlist = bool(add_to_shortlist)
            draft.publish_idempotency_key = key
            draft.published_opportunity_id = opportunity.id
            draft.status = "PUBLISHED"
            draft.validation_errors_json = None

            record_imported_history(db, opportunity)
            lane_match_count = refresh_opportunity_lane_matches(
                db, draft.organization_id, opportunity
            )
            added = False
            if add_to_shortlist is True:
                added = ensure_user_shortlisted(
                    db, opportunity=opportunity, user=publishing_user
                )
            _record_ingestion_audit(
                db,
                draft=draft,
                opportunity=opportunity,
                user=publishing_user,
                now=current_time,
                material_count=material_count,
            )
            db.flush()
            db.commit()
            return OpportunityPublishResult(
                opportunity_id=opportunity.id,
                source_record_id=opportunity.source_record_id,
                solicitation_number=opportunity.solicitation_number,
                added_to_shortlist=added,
                metadata={
                    "idempotent_replay": False,
                    "source_material_count": material_count,
                    "pursuit_lane_match_count": lane_match_count,
                    "probable_duplicates": [asdict(match) for match in duplicates.probable_matches],
                },
            )
        except OpportunityPublicationError:
            db.rollback()
            raise
        except IntegrityError as exc:
            db.rollback()
            replay = db.query(OpportunityIntakeDraft).filter(
                OpportunityIntakeDraft.id == draft_id,
                OpportunityIntakeDraft.publish_idempotency_key == key,
                OpportunityIntakeDraft.published_opportunity_id.isnot(None),
            ).one_or_none()
            if replay is not None:
                result = _result_for(db, replay, replay.published_opportunity)
                db.commit()
                return result
            raise OpportunityPublicationConflict("Opportunity publication conflicted with another request") from exc
        except Exception:
            db.rollback()
            raise
