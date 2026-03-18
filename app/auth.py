"""Session-based authentication."""
import bcrypt
from starlette.requests import Request
from app import config


def verify_password(plain: str) -> bool:
    if not config.APP_PASSWORD_HASH:
        return True  # auth disabled (local dev)
    try:
        return bcrypt.checkpw(plain.encode(), config.APP_PASSWORD_HASH.encode())
    except Exception:
        return False


def is_authenticated(request: Request) -> bool:
    if not config.APP_PASSWORD_HASH:
        return True
    return request.session.get("authenticated") is True
