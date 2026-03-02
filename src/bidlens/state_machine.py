# src/bidlens/state_machine.py
from enum import StrEnum
from typing import Dict, Set


class OppState(StrEnum):
    INBOX = "INBOX"
    SHORTLISTED = "SHORTLISTED"
    ARCHIVED = "ARCHIVED"


ALLOWED_TRANSITIONS: Dict[OppState, Set[OppState]] = {
    OppState.INBOX: {OppState.SHORTLISTED, OppState.ARCHIVED},
    OppState.SHORTLISTED: {OppState.INBOX, OppState.ARCHIVED},
    OppState.ARCHIVED: {OppState.INBOX, OppState.SHORTLISTED},
}


def validate_transition(from_state: OppState, to_state: OppState) -> None:
    allowed = ALLOWED_TRANSITIONS.get(from_state, set())
    if to_state not in allowed:
        raise ValueError(f"Invalid transition: {from_state} -> {to_state}")
