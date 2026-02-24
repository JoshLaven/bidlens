from itsdangerous import URLSafeSerializer
from fastapi import Depends, Request, Response
from sqlalchemy.orm import Session

from .database import get_db
from .config import SECRET_KEY, SESSION_COOKIE_NAME
from .models import User

serializer = URLSafeSerializer(SECRET_KEY)

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
            return db.query(User).filter(User.id == user_id).first()
    except Exception:
        pass
    return None
    
def org_is_active(user):
    return user.organization and user.organization.is_active

def clear_session(response: Response):
    response.delete_cookie(SESSION_COOKIE_NAME)
