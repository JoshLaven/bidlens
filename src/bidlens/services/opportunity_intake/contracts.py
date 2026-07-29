from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Mapping


INTAKE_SOURCE = "user_intake"
INTAKE_QUALIFICATION_STATUS = "qualified"
INTAKE_DECISION_STATE = "INBOX"
DEFAULT_ADD_TO_SHORTLIST = True
DEFAULT_OPPORTUNITY_TYPE = "RFP"


class IntakeMethod(str, Enum):
    MANUAL = "manual"
    DOCUMENT = "document"
    EMAIL = "email"


@dataclass(frozen=True)
class IntakeCandidate:
    """Editable opportunity values proposed before publication."""

    title: str | None = None
    client: str | None = None
    response_deadline: date | None = None
    solicitation_number: str | None = None
    opportunity_type: str | None = None
    description: str | None = None
    source_url: str | None = None
    naics: str | None = None
    naics_title: str | None = None
    set_aside: str | None = None


@dataclass(frozen=True)
class OpportunityPublishCommand:
    """Provider-independent input to the future transactional publisher."""

    organization_id: int
    workspace_id: int
    user_id: int
    intake_method: IntakeMethod
    candidate: IntakeCandidate
    add_to_shortlist: bool = DEFAULT_ADD_TO_SHORTLIST
    source_material_ids: tuple[int, ...] = ()
    idempotency_key: str | None = None


@dataclass(frozen=True)
class OpportunityPublishResult:
    opportunity_id: int
    source_record_id: str
    solicitation_number: str
    added_to_shortlist: bool
    qualification_status: str = INTAKE_QUALIFICATION_STATUS
    decision_state: str = INTAKE_DECISION_STATE
    metadata: Mapping[str, Any] = field(default_factory=dict)
