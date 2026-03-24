"""Session-based authentication."""
import base64
import bcrypt
from starlette.requests import Request
from app import config


def verify_password(plain: str) -> bool:
    if not config.APP_PASSWORD_HASH:
        return True  # auth disabled (local dev)
    try:
        stored = base64.b64decode(config.APP_PASSWORD_HASH.encode())
        return bcrypt.checkpw(plain.encode(), stored)
    except Exception:
        return False


def is_authenticated(request: Request) -> bool:
    if not config.APP_PASSWORD_HASH:
        return True
    return request.session.get("authenticated") is True
