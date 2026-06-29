"""Server-side login sessions with sliding-window inactivity expiry (Phase 16).

A session is valid while it has been used within `session_inactivity_days`. Every authenticated request
calls resolve(), which bumps last_seen_at (throttled), so an active student stays signed in while one
who is away for 2 whole days is auto-logged-out. The bearer token IS the session id (opaque random).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .. import models
from ..config import settings
from . import security


def create(db, account: "models.Account") -> str:
    token = security.new_session_token()
    now = datetime.utcnow()
    db.add(models.AuthSession(id=token, account_id=account.id, created_at=now, last_seen_at=now))
    db.commit()
    return token


def resolve(db, token: str):
    """Account for a live session (and refresh it), or None if missing / revoked / idle-expired."""
    s = db.get(models.AuthSession, token)
    if s is None or s.revoked:
        return None
    now = datetime.utcnow()
    if (now - s.last_seen_at) > timedelta(days=settings.session_inactivity_days):
        return None                       # idle too long -> auto-logout
    if (now - s.last_seen_at).total_seconds() > 60:   # throttle writes to ~once/min
        s.last_seen_at = now
        db.commit()
    return db.get(models.Account, s.account_id)


def revoke(db, token: str) -> None:
    s = db.get(models.AuthSession, token)
    if s is not None and not s.revoked:
        s.revoked = True
        db.commit()
