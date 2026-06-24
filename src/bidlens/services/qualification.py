from sqlalchemy.orm import Session

from ..models import OrgProfile


QUALIFICATION_UNREVIEWED = "unreviewed"
QUALIFICATION_QUALIFIED = "qualified"


def triage_enabled_for_org(db: Session, organization_id: int) -> bool:
    profile = db.query(OrgProfile).filter(OrgProfile.org_id == organization_id).first()
    return bool(profile and profile.triage_enabled)


def new_opportunity_qualification_status(db: Session, organization_id: int) -> str:
    if triage_enabled_for_org(db, organization_id):
        return QUALIFICATION_UNREVIEWED
    return QUALIFICATION_QUALIFIED
