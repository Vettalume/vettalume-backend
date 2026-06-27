from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..deps import get_current_learner, get_db
from ..schemas import DevLoginIn, TokenOut
from ..services import security

router = APIRouter(prefix="/auth", tags=["auth"])

MIN_PASSWORD_LEN = 8


class RegisterIn(BaseModel):
    email: str
    password: str
    display_name: str | None = None
    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return v.strip().lower()


class LoginIn(BaseModel):
    email: str
    password: str
    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return v.strip().lower()


def _auth_response(acct: models.Account) -> dict:
    return {"access_token": security.make_token(acct.id), "token_type": "bearer",
            "learner_id": str(acct.id),
            "account": {"id": str(acct.id), "email": acct.email, "display_name": acct.display_name}}


@router.post("/register")
def register(body: RegisterIn, db: Session = Depends(get_db)) -> dict:
    """Create an account with a password and return a Bearer JWT."""
    if len(body.password) < MIN_PASSWORD_LEN:
        raise HTTPException(400, f"password must be at least {MIN_PASSWORD_LEN} characters")
    if db.scalar(select(models.Account).where(models.Account.email == body.email)) is not None:
        raise HTTPException(409, "email already registered")
    acct = models.Account(email=body.email,
                          display_name=body.display_name or body.email.split("@")[0])
    db.add(acct)
    db.flush()
    db.add(models.Credential(account_id=acct.id,
                             password_hash=security.hash_password(body.password)))
    db.commit()
    return _auth_response(acct)


def _touch_last_login(db: Session, acct: "models.Account") -> None:
    """Record this sign-in time on the student's profile (get-or-create the row)."""
    prof = db.get(models.StudentProfile, acct.id)
    if prof is None:
        prof = models.StudentProfile(account_id=acct.id)
        db.add(prof)
    prof.last_login = datetime.now().strftime("%Y-%m-%d %H:%M")
    db.commit()


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)) -> dict:
    """Verify email + password and return a Bearer JWT."""
    acct = db.scalar(select(models.Account).where(models.Account.email == body.email))
    cred = db.get(models.Credential, acct.id) if acct else None
    # one message for both failure modes -> no account enumeration
    if acct is None or cred is None or not security.verify_password(body.password, cred.password_hash):
        raise HTTPException(401, "invalid email or password")
    _touch_last_login(db, acct)
    return _auth_response(acct)


@router.get("/me")
def me(learner=Depends(get_current_learner)) -> dict:
    return {"id": str(learner.id), "email": learner.email, "display_name": learner.display_name}


@router.post("/dev-login", response_model=TokenOut)
def dev_login(body: DevLoginIn, db: Session = Depends(get_db)) -> TokenOut:
    """Dev convenience: create-or-get an account by email (no password) and auto-grant an entitlement.
    Returns a real Bearer JWT as access_token, plus learner_id for the legacy X-Learner-Id header.
    Disabled when dev_mode is off."""
    if not settings.dev_mode:
        raise HTTPException(403, "dev-login is disabled (dev_mode is off) — use /auth/register")
    acct = db.scalar(select(models.Account).where(models.Account.email == body.email))
    if acct is None:
        acct = models.Account(email=body.email,
                              display_name=body.display_name or body.email.split("@")[0])
        db.add(acct)
        db.flush()

    ent = db.scalar(select(models.Entitlement).where(
        models.Entitlement.account_id == acct.id, models.Entitlement.exam_code == body.exam_code))
    if ent is None and db.get(models.Exam, body.exam_code) is not None:
        db.add(models.Entitlement(account_id=acct.id, exam_code=body.exam_code, status="active"))

    _touch_last_login(db, acct)
    db.commit()
    return TokenOut(access_token=security.make_token(acct.id), learner_id=str(acct.id))
