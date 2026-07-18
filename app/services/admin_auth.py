"""Admin authorization — the content perimeter.

An account is an admin iff (a) its email is listed in settings.admin_emails (env ADMIN_EMAILS), or
(b) it has an AdminUser row. `require_admin` is the FastAPI dependency that every content-authoring
endpoint sits behind: it returns 401 for anyone unauthenticated and 403 for an authenticated
non-admin, so outsiders can neither read nor write the syllabus or item bank.

Security note: require_admin deliberately accepts ONLY a real Bearer JWT (from /auth/login,
/auth/register, or dev-login). It does NOT honour the legacy passwordless X-Learner-Id header — that
header lets a caller claim any account id, which would otherwise be an admin-impersonation hole.
"""
from __future__ import annotations

import uuid

from fastapi import Cookie, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..deps import get_db
from . import security, sessions


def _admin_email_set() -> set[str]:
    return {e.strip().lower() for e in (settings.admin_emails or "").split(",") if e.strip()}


def is_admin(db: Session, account: models.Account | None) -> bool:
    if account is None:
        return False
    if account.email and account.email.lower() in _admin_email_set():
        return True
    return db.get(models.AdminUser, account.id) is not None


def require_admin(
    authorization: str | None = Header(None, description="Bearer <JWT> for an admin account"),
    user_agent: str | None = Header(None),
    vls_session: str | None = Cookie(None),
    db: Session = Depends(get_db),
) -> models.Account:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    elif vls_session:
        token = vls_session.strip()
    if not token:
        raise HTTPException(status_code=401, detail="admin login required")
    # New sliding-session tokens are server-side, device-bound and unforgeable (you can't claim another
    # account's id, unlike the rejected X-Learner-Id header), so they're safe to accept here.
    if token.startswith("vls_"):
        acc = sessions.resolve(db, token, user_agent)
        if acc is None:
            raise HTTPException(status_code=401, detail="session expired or invalid")
    else:
        try:
            payload = security.decode_token(token)
        except ValueError:
            raise HTTPException(status_code=401, detail="invalid or expired token")
        sub = payload.get("sub")
        try:
            acc = db.get(models.Account, uuid.UUID(str(sub)))
        except (ValueError, TypeError):
            acc = None
    if acc is None:
        raise HTTPException(status_code=401, detail="account not found")
    if not is_admin(db, acc):
        raise HTTPException(status_code=403, detail="admin access required")
    return acc


def ensure_admins(db: Session) -> int:
    """Promote any existing account whose email is in admin_emails to an AdminUser row (idempotent).
    Called on boot. Does NOT create accounts — only grants the role to ones that already exist."""
    emails = _admin_email_set()
    if not emails:
        return 0
    n = 0
    for acc in db.scalars(select(models.Account)).all():
        if acc.email and acc.email.lower() in emails and db.get(models.AdminUser, acc.id) is None:
            db.add(models.AdminUser(account_id=acc.id, role="admin"))
            n += 1
    if n:
        db.commit()
    return n


def grant_admins_full_access(db: Session) -> int:
    """Every admin (an AdminUser row or an ADMIN_EMAILS account) gets an active entitlement for every
    exam, so they can access all content/mocks regardless of enforcement. Idempotent; runs on boot."""
    admin_ids = {a.account_id for a in db.scalars(select(models.AdminUser)).all()}
    emails = _admin_email_set()
    if emails:
        for acc in db.scalars(select(models.Account)).all():
            if acc.email and acc.email.lower() in emails:
                admin_ids.add(acc.id)
    exams = [e.code for e in db.scalars(select(models.Exam)).all()]
    n = 0
    for aid in admin_ids:
        for ex in exams:
            ent = db.scalar(select(models.Entitlement).where(
                models.Entitlement.account_id == aid, models.Entitlement.exam_code == ex))
            if ent is None:
                db.add(models.Entitlement(account_id=aid, exam_code=ex, status="active")); n += 1
            elif ent.status != "active":
                ent.status = "active"; n += 1
    if n:
        db.commit()
    return n


def grant_admin(db: Session, email: str) -> models.Account:
    acc = db.scalar(select(models.Account).where(models.Account.email == email.strip().lower()))
    if acc is None:
        raise HTTPException(status_code=404, detail=f"no account with email {email!r} — they must register first")
    if db.get(models.AdminUser, acc.id) is None:
        db.add(models.AdminUser(account_id=acc.id, role="admin"))
        db.commit()
    return acc


def revoke_admin(db: Session, account_id) -> bool:
    aid = account_id if isinstance(account_id, uuid.UUID) else uuid.UUID(str(account_id))
    row = db.get(models.AdminUser, aid)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True
