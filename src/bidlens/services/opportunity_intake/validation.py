from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .contracts import IntakeCandidate
from .normalization import normalize_candidate


@dataclass(frozen=True)
class ValidationError:
    field: str
    code: str
    message: str


@dataclass(frozen=True)
class IntakeValidationResult:
    candidate: IntakeCandidate
    errors: tuple[ValidationError, ...]
    requires_internal_reference: bool

    @property
    def is_valid(self) -> bool:
        return not self.errors


def validate_candidate(
    values: IntakeCandidate | Mapping[str, Any],
) -> IntakeValidationResult:
    candidate = normalize_candidate(values)
    errors: list[ValidationError] = []
    if not candidate.title:
        errors.append(ValidationError("title", "required", "Opportunity Title is required."))
    if not candidate.client:
        errors.append(ValidationError("client", "required", "Client is required."))
    if not candidate.response_deadline:
        errors.append(
            ValidationError(
                "response_deadline",
                "required_or_invalid",
                "Enter a valid Response Deadline.",
            )
        )
    return IntakeValidationResult(
        candidate=candidate,
        errors=tuple(errors),
        requires_internal_reference=not bool(candidate.solicitation_number),
    )
