"""Password hashing (bcrypt) and JWT creation/validation (PyJWT).

Design notes:
  - bcrypt directly (passlib is unmaintained); bcrypt truncates at
    72 bytes, enforced at the schema layer so users get a clear error.
  - Two token types, both HS256 JWTs: short-lived `access` and
    long-lived `refresh`. `decode_token` hard-fails on a type mismatch
    so a refresh token can never be replayed as an access token.
  - `jti` is included so a Redis revocation blocklist can be added
    later without reissuing the token format.
"""
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.core.config import settings

ACCESS = "access"
REFRESH = "refresh"


class TokenError(Exception):
    """Raised for any invalid/expired/mistyped token."""


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def _create_token(subject: str, token_type: str, lifetime: timedelta) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": now + lifetime,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_access_token(user_id: uuid.UUID) -> str:
    return _create_token(str(user_id), ACCESS,
                         timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))


def create_refresh_token(user_id: uuid.UUID) -> str:
    return _create_token(str(user_id), REFRESH,
                         timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS))


def decode_token(token: str, expected_type: str) -> dict:
    """Validate signature, expiry, and token type. Returns the payload."""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise TokenError(f"invalid token: {exc}") from exc
    if payload.get("type") != expected_type:
        raise TokenError(f"expected {expected_type} token, got {payload.get('type')}")
    if "sub" not in payload:
        raise TokenError("token missing subject")
    return payload
