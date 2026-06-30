from itsdangerous import URLSafeSerializer
from fastapi import Depends, Request, Response
from sqlalchemy.orm import Session

from .database import get_db
from .config import SECRET_KEY, SESSION_COOKIE_NAME
from .models import Opportunity, OrganizationMembership, User
from .tenancy import current_organization, ensure_email_domain_membership
from .services.qualification import triage_enabled_for_org

serializer = URLSafeSerializer(SECRET_KEY)


def attach_request_user_context(request: Request, db: Session, user: User) -> User:
    org = current_organization(request, db, user)
    membership = (
        db.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == org.id,
            OrganizationMembership.user_id == user.id,
        )
        .first()
    )
    triage_unreviewed_count = (
        db.query(Opportunity)
        .filter(Opportunity.organization_id == org.id)
        .filter(Opportunity.decision_state != "ARCHIVED")
        .filter(Opportunity.qualification_status == "unreviewed")
        .count()
    )
    setattr(user, "current_organization_id", org.id)
    setattr(user, "current_organization_name", org.name)
    setattr(user, "current_role", membership.role if membership else "member")
    setattr(user, "triage_enabled", triage_enabled_for_org(db, org.id))
    setattr(user, "triage_unreviewed_count", triage_unreviewed_count)
    return user

def create_session(response: Response, user_id: int):
    token = serializer.dumps({"user_id": user_id})
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        max_age=60 * 60 * 24 * 30,
        samesite="lax"
    )

def get_current_user(request: Request, db: Session=Depends(get_db),) -> User | None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    try:
        data = serializer.loads(token)
        user_id = data.get("user_id")
        if user_id:
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                matched_org = ensure_email_domain_membership(db, user)
                if matched_org:
                    db.commit()
                try:
                    attach_request_user_context(request, db, user)
                except Exception:
                    pass
            return user
    except Exception:
        pass
    return None
    
def org_is_active(user):
    return user.organization and user.organization.is_active

def clear_session(response: Response):
    response.delete_cookie(SESSION_COOKIE_NAME)
