from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User
from passlib.context import CryptContext

_pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def seed_if_needed(db: Session) -> None:
    maria = db.scalars(select(User).where(User.username == "Maria")).first()
    if not maria:
        db.add(
            User(
                username="Maria",
                password_hash=_pwd.hash("roofquote123"),
            )
        )
        db.commit()
        return
    # bcrypt hashes from older seeds break with bcrypt 4.1+ / passlib; rehash once
    if maria.password_hash.startswith("$2"):
        maria.password_hash = _pwd.hash("roofquote123")
        db.commit()
