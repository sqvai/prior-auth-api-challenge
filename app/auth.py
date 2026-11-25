# Copyright (c) 2026 John Harris. All rights reserved. Proprietary.
"""Tokens stand in for the company identity service.

The token carries the user id and role. We still load the user and require the
role in the token to match the row, so a stale token cannot keep a role we removed.

This is HMAC, not a JWT library, so there is no algorithm header to confuse.
"""

import base64
import hashlib
import hmac
import json
import time
import uuid

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.errors import ApiError
from app.models import User

_bearer = HTTPBearer(auto_error=False)


class AuthError(Exception):
    pass


def issue_token(user_id: uuid.UUID, role: str, secret: str, ttl_seconds: int) -> str:
    payload = {"sub": str(user_id), "role": role, "exp": int(time.time()) + ttl_seconds}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    signature = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"v1.{body}.{signature}"


def decode_token(token: str, secret: str) -> dict:
    if len(token) > 4096:
        raise AuthError()
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "v1":
        raise AuthError()
    body, signature = parts[1], parts[2]
    expected = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, signature):
        raise AuthError()
    try:
        payload = json.loads(_unb64(body))
        exp = int(payload["exp"])
        uuid.UUID(str(payload["sub"]))
        role = str(payload["role"])
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise AuthError() from exc
    if exp < int(time.time()):
        raise AuthError()
    payload["role"] = role
    return payload


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise ApiError(401, "unauthorized", "Send Authorization: Bearer <token> from the identity service.")
    try:
        payload = decode_token(credentials.credentials, settings.token_secret)
    except AuthError:
        raise ApiError(401, "unauthorized", "That token is missing, expired, or not signed by this service.") from None
    user = db.get(User, uuid.UUID(payload["sub"]))
    if user is None or user.role != payload["role"]:
        raise ApiError(401, "unauthorized", "That token does not match the caller's current role.")
    return user


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
