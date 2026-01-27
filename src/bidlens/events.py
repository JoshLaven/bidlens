# src/bidlens/events.py
from sqlalchemy.orm import Session
from .models import Event

def log_event(
    db: Session,
    *,
    event_type: str,
    org_id: int | None,
    user_id: int | None,
    opp_id: int | None,
    payload: dict,
    ui_version: str = "v1",
) -> None:
    e = Event(
        event_type=event_type,
        org_id=org_id,
        user_id=user_id,
        opp_id=opp_id,
        payload=payload or {},
        ui_version=ui_version or "v1",
    )
    db.add(e)
    db.commit()
