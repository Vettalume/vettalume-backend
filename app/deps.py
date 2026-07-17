from __future__ import annotations

import uuid

from fastapi import Cookie, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal
from .models import Account
from .services import security, sessions


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _account_by_id(db: Session, raw) -> Account:
    try:
        acct = db.get(Account, uuid.UUID(str(raw)))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=401, detail="invalid account id")
    if acct is None:
        raise HTTPException(status_code=401, detail="unknown account")
    return acct


def get_current_learner(
    authorization: str | None = Header(None, description="Bearer <JWT> from /auth/login or /auth/register"),
    x_learner_id: str | None = Header(None, description="Legacy dev auth (the learner_id from /auth/dev-login)"),
    user_agent: str | None = Header(None),
    vls_session: str | None = Cookie(None),
    db: Session = Depends(get_db),
) -> Account:
    """Resolve the calling learner. The session token comes from the Authorization Bearer header, or
    (once cookie auth is enabled) the HttpOnly vls_session cookie. The Phase-0 X-Learner-Id header is
    still accepted for dev unless settings.require_jwt is on (then JWT is mandatory)."""
    token: str | None = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    elif vls_session:
        token = vls_session.strip()

    if token:
        # New student auth issues opaque session tokens (device-bound, sliding expiry + 7-day cap).
        if token.startswith("vls_"):
            acct = sessions.resolve(db, token, user_agent)
            if acct is None:
                raise HTTPException(status_code=401, detail="session expired or invalid")
            return acct
        # Otherwise it's a JWT from the existing flows (register / dev-login).
        try:
            payload = security.decode_token(token)
        except ValueError:
            raise HTTPException(status_code=401, detail="invalid or expired token")
        return _account_by_id(db, payload.get("sub"))

    if not settings.require_jwt and x_learner_id:
        return _account_by_id(db, x_learner_id)

    raise HTTPException(status_code=401, detail="authentication required (Bearer token)")
