from fastapi import Request
from sqlalchemy.orm import Session

from app.models import User


def session_user(request: Request, db: Session) -> User | None:
    uid = request.session.get("user_id")
    if not uid:
        return None
    return db.get(User, int(uid))
