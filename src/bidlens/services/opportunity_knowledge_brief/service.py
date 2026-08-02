"""Application-facing Get Up to Speed generation service."""

from __future__ import annotations

import logging
from time import perf_counter
from typing import Any

from sqlalchemy.orm import Session

from ... import config
from ...models import OpportunityKnowledgeBriefGeneration, User
from .access_policy import GUTSAccessError, require_guts_generation_access
from .compiler import GUTSCompilerError, OpportunityKnowledgeBriefCompiler
from .prompt import GUTSPromptConfigurationError, resolve_prompt
from .repository import (
    ActiveKnowledgeBriefGenerationError, create_pending_generation, expire_stale_generation,
    get_active_generation,
)


logger = logging.getLogger(__name__)


class GUTSServiceError(RuntimeError):
    def __init__(
        self, safe_category: str, safe_message: str, *, stage: str,
        retryable: bool = False, generation_id: int | None = None,
        validation_debug: dict[str, Any] | None = None,
        provider_debug: dict[str, Any] | None = None,
        schema_debug: dict[str, Any] | None = None,
    ):
        super().__init__(safe_message)
        self.safe_category = safe_category
        self.safe_message = safe_message
        self.stage = stage
        self.retryable = retryable
        self.generation_id = generation_id
        self.validation_debug = validation_debug
        self.provider_debug = provider_debug
        self.schema_debug = schema_debug


class OpportunityKnowledgeBriefService:
    def __init__(self, db: Session, *, compiler=None):
        self.db = db
        self.compiler = compiler

    def generate(
        self, *, opportunity_id: int, requesting_user: User,
        active_organization_id: int,
    ) -> OpportunityKnowledgeBriefGeneration:
        if not config.GUTS_ENABLED:
            raise GUTSServiceError("access_denied", "Get Up to Speed is not enabled.", stage="authorization")
        try:
            prompt = resolve_prompt(config.GUTS_PROMPT_VERSION)
            if prompt.output_schema_version != config.GUTS_OUTPUT_SCHEMA_VERSION:
                raise GUTSPromptConfigurationError(
                    GUTSPromptConfigurationError.safe_message,
                )
        except GUTSPromptConfigurationError as exc:
            raise GUTSServiceError(
                exc.safe_category, exc.safe_message, stage="configuration",
            ) from None
        authorization_started = perf_counter()
        try:
            context = require_guts_generation_access(
                self.db, user=requesting_user, opportunity_id=opportunity_id,
            )
        except GUTSAccessError as exc:
            raise GUTSServiceError(exc.failure_category, str(exc), stage="authorization") from None
        if context.organization_id != active_organization_id:
            raise GUTSServiceError("access_denied", "Opportunity organization context is unavailable.", stage="authorization")
        authorization_ms = round((perf_counter() - authorization_started) * 1000)

        active = get_active_generation(
            self.db, organization_id=context.organization_id, opportunity_id=opportunity_id,
        )
        if active and not expire_stale_generation(
            self.db, active, max_age_seconds=config.GUTS_STALE_ATTEMPT_SECONDS,
        ):
            raise GUTSServiceError(
                "generation_already_in_progress", "A briefing is already being generated.",
                stage="lifecycle", retryable=True, generation_id=active.id,
            )
        try:
            generation = create_pending_generation(
                self.db, organization_id=context.organization_id, workspace_id=context.workspace_id,
                opportunity_id=opportunity_id, generated_by_user_id=requesting_user.id,
            )
        except ActiveKnowledgeBriefGenerationError:
            active = get_active_generation(
                self.db, organization_id=context.organization_id, opportunity_id=opportunity_id,
            )
            raise GUTSServiceError(
                "generation_already_in_progress", "A briefing is already being generated.",
                stage="lifecycle", retryable=True, generation_id=active.id if active else None,
            ) from None
        compiler = self.compiler or OpportunityKnowledgeBriefCompiler(self.db)
        try:
            return compiler.generate(
                generation=generation, access_context=context, authorization_ms=authorization_ms,
            )
        except GUTSCompilerError as exc:
            raise GUTSServiceError(
                exc.safe_category, exc.safe_message, stage=exc.stage,
                retryable=exc.retryable, generation_id=generation.id,
                validation_debug=exc.validation_debug,
                provider_debug=exc.provider_debug,
                schema_debug=exc.schema_debug,
            ) from None
