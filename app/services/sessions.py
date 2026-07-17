"""Server-side login sessions with sliding-window inactivity expiry + an absolute cap (Phase 16).

A session is valid while it has been used within `session_inactivity_days` (idle window, bumped on
every request) AND is younger than `session_max_days` (absolute cap from login). So an active student
stays signed in until the 7-day cap; one who is away/asleep for ~a day is auto-logged-out sooner. The
bearer token IS the session id (opaque random).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from sqlalchemy import update

from .. import models
from ..config import settings
from . import security


def fingerprint(user_agent: str | None) -> str | None:
    """Stable device fingerprint from the User-Agent (sha256 hex), or None if no UA was sent."""
    ua = (user_agent or "").strip()
    return hashlib.sha256(ua.encode("utf-8")).hexdigest() if ua else None


def create(db, account: "models.Account", user_agent: str | None = None) -> str:
    token = security.new_session_token()
    now = datetime.utcnow()
    db.add(models.AuthSession(id=token, account_id=account.id, created_at=now, last_seen_at=now,
                              ua_hash=fingerprint(user_agent)))
    db.commit()
    return token


def resolve(db, token: str, user_agent: str | None = None):
    """Account for a live session (and refresh it), or None if missing / revoked / expired / hijacked.

    Two independent expiries: an absolute cap from login (`session_max_days`, unaffected by activity)
    and a sliding idle window (`session_inactivity_days`, reset on every request). Active use keeps a
    session alive until the absolute cap; going idle (e.g. sleep / a day away) auto-logs-out sooner.

    Device binding: the session is tied to the browser that first used it. A token presented from a
    different browser (a copied/stolen token replayed elsewhere) fails the fingerprint check and is
    rejected — even while it would otherwise still be valid."""
    s = db.get(models.AuthSession, token)
    if s is None or s.revoked:
        return None
    now = datetime.utcnow()
    if (now - s.created_at) > timedelta(days=settings.session_max_days):
        return None                       # absolute max age -> must re-login even if active
    if (now - s.last_seen_at) > timedelta(days=settings.session_inactivity_days):
        return None                       # idle too long -> auto-logout

    fp = fingerprint(user_agent)
    dirty = False
    if s.ua_hash:
        if fp != s.ua_hash:
            return None                   # different browser (or no UA) -> stolen-token replay
    elif fp:
        s.ua_hash = fp                    # bind a pre-existing session to its browser on first use
        dirty = True

    if (now - s.last_seen_at).total_seconds() > 60:   # throttle writes to ~once/min
        s.last_seen_at = now
        dirty = True
    if dirty:
        db.commit()
    return db.get(models.Account, s.account_id)


def revoke(db, token: str) -> None:
    s = db.get(models.AuthSession, token)
    if s is not None and not s.revoked:
        s.revoked = True
        db.commit()


def revoke_all(db, account_id, *, keep_token: str | None = None) -> int:
    """Revoke every live session for an account (e.g. after a password change / "log out everywhere").
    Pass keep_token to spare the caller's current session. Returns the number of sessions revoked."""
    stmt = (update(models.AuthSession)
            .where(models.AuthSession.account_id == account_id, models.AuthSession.revoked == False)  # noqa: E712
            .values(revoked=True))
    if keep_token is not None:
        stmt = stmt.where(models.AuthSession.id != keep_token)
    result = db.execute(stmt)
    db.commit()
    return result.rowcount or 0
