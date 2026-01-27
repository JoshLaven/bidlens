# src/bidlens/state_machine.py
from enum import StrEnum
from typing import Dict, Set

class OppState(StrEnum):
    FEED = "FEED"
    SAVED = "SAVED"
    BID = "BID"
    NO_BID = "NO_BID"

TERMINAL_STATES: Set[OppState] = {OppState.BID, OppState.NO_BID}

ALLOWED_TRANSITIONS: Dict[OppState, Set[OppState]] = {
    OppState.FEED: {OppState.SAVED, OppState.NO_BID},
    OppState.SAVED: {OppState.BID, OppState.NO_BID},
    OppState.BID: set(),
    OppState.NO_BID: set(),
}

def is_terminal(state: OppState) -> bool:
    return state in TERMINAL_STATES

def validate_transition(from_state: OppState, to_state: OppState) -> None:
    allowed = ALLOWED_TRANSITIONS.get(from_state, set())
    if to_state not in allowed:
        raise ValueError(f"Invalid transition: {from_state} -> {to_state}")
