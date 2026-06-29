from __future__ import annotations

import uuid

from fastapi import Depends, Header, HTTPException
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
    db: Session = Depends(get_db),
) -> Account:
    """Resolve the calling learner. A real Bearer JWT is preferred; the Phase-0 X-Learner-Id header is
    still accepted for dev unless settings.require_jwt is on (then JWT is mandatory)."""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        # New student auth issues opaque session tokens (sliding 2-day inactivity expiry).
        if token.startswith("vls_"):
            acct = sessions.resolve(db, token)
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
