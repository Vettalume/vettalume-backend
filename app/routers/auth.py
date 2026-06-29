from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..deps import get_current_learner, get_db
from ..schemas import DevLoginIn, TokenOut
from ..services import email as email_svc
from ..services import google_auth, security, sessions

router = APIRouter(prefix="/auth", tags=["auth"])

MIN_PASSWORD_LEN = 8


def _norm(v: str) -> str:
    return (v or "").strip().lower()


# ----------------------------- legacy schemas/endpoints (unchanged) -----------------------------
class RegisterIn(BaseModel):
    email: str
    password: str
    display_name: str | None = None
    @field_validator("email")
    @classmethod
    def _e(cls, v): return _norm(v)


class LoginIn(BaseModel):
    email: str
    password: str
    @field_validator("email")
    @classmethod
    def _e(cls, v): return _norm(v)


def _auth_response(acct: models.Account) -> dict:
    """Legacy JWT response (kept for /register and existing integrations)."""
    return {"access_token": security.make_token(acct.id), "token_type": "bearer",
            "learner_id": str(acct.id),
            "account": {"id": str(acct.id), "email": acct.email, "display_name": acct.display_name}}


def _session_response(db: Session, acct: models.Account) -> dict:
    """New sliding-session response: an opaque token valid until 2 idle days pass."""
    token = sessions.create(db, acct)
    return {"access_token": token, "token_type": "bearer", "learner_id": str(acct.id),
            "account": {"id": str(acct.id), "email": acct.email, "display_name": acct.display_name}}


@router.post("/register")
def register(body: RegisterIn, db: Session = Depends(get_db)) -> dict:
    """Create an account with a password and return a Bearer JWT. (Legacy/dev — the student flow is /signup.)"""
    if len(body.password) < MIN_PASSWORD_LEN:
        raise HTTPException(400, f"password must be at least {MIN_PASSWORD_LEN} characters")
    if db.scalar(select(models.Account).where(models.Account.email == body.email)) is not None:
        raise HTTPException(409, "email already registered")
    acct = models.Account(email=body.email, display_name=body.display_name or body.email.split("@")[0])
    db.add(acct); db.flush()
    db.add(models.Credential(account_id=acct.id, password_hash=security.hash_password(body.password)))
    db.commit()
    return _auth_response(acct)


def _touch_last_login(db: Session, acct: "models.Account") -> None:
    prof = _profile(db, acct)
    prof.last_login = datetime.now().strftime("%Y-%m-%d %H:%M")
    db.commit()


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)) -> dict:
    """Email + password sign-in. Returns a 2-day sliding session token. New-flow accounts must have
    verified their email first; legacy accounts (no auth metadata) are grandfathered in."""
    acct = db.scalar(select(models.Account).where(models.Account.email == body.email))
    cred = db.get(models.Credential, acct.id) if acct else None
    if acct is None or cred is None or not security.verify_password(body.password, cred.password_hash):
        raise HTTPException(401, "invalid email or password")   # one message -> no account enumeration
    meta = db.get(models.AccountAuth, acct.id)
    prof = db.get(models.StudentProfile, acct.id)
    if meta is not None and meta.provider == "password" and prof is not None and not prof.verified:
        raise HTTPException(403, {"error": "email_not_verified",
                                  "detail": "Please verify your email before signing in."})
    _touch_last_login(db, acct)
    return _session_response(db, acct)


@router.get("/me")
def me(learner=Depends(get_current_learner)) -> dict:
    return {"id": str(learner.id), "email": learner.email, "display_name": learner.display_name}


@router.post("/dev-login", response_model=TokenOut)
def dev_login(body: DevLoginIn, db: Session = Depends(get_db)) -> TokenOut:
    """Dev convenience: create-or-get an account by email (no password) + auto-grant an entitlement."""
    if not settings.dev_mode:
        raise HTTPException(403, "dev-login is disabled (dev_mode is off) — use /auth/register")
    acct = db.scalar(select(models.Account).where(models.Account.email == body.email))
    if acct is None:
        acct = models.Account(email=body.email, display_name=body.display_name or body.email.split("@")[0])
        db.add(acct); db.flush()
    ent = db.scalar(select(models.Entitlement).where(
        models.Entitlement.account_id == acct.id, models.Entitlement.exam_code == body.exam_code))
    if ent is None and db.get(models.Exam, body.exam_code) is not None:
        db.add(models.Entitlement(account_id=acct.id, exam_code=body.exam_code, status="active"))
    _touch_last_login(db, acct)
    db.commit()
    return TokenOut(access_token=security.make_token(acct.id), learner_id=str(acct.id))


# ----------------------------- student signup + verification (new) -----------------------------
def _profile(db: Session, acct: models.Account) -> models.StudentProfile:
    p = db.get(models.StudentProfile, acct.id)
    if p is None:
        p = models.StudentProfile(account_id=acct.id)
        db.add(p)
    return p


def _issue_otp(db: Session, acct: models.Account, *, cooldown: bool = False) -> dict:
    """Generate, store (hashed), and email a fresh OTP. Enforces the resend cooldown when asked."""
    now = datetime.utcnow()
    row = db.get(models.EmailOtp, acct.id)
    if cooldown and row is not None:
        wait = settings.otp_resend_cooldown_seconds - (now - row.last_sent_at).total_seconds()
        if wait > 0:
            raise HTTPException(429, {"error": "resend_cooldown", "retry_after": int(wait) + 1,
                                      "detail": f"Please wait {int(wait) + 1}s before requesting another code."})
    code = security.new_otp()
    if row is None:
        db.add(models.EmailOtp(account_id=acct.id, code_hash=security.hash_otp(code),
                               expires_at=now + timedelta(seconds=settings.otp_ttl_seconds),
                               attempts=0, last_sent_at=now))
    else:
        row.code_hash = security.hash_otp(code)
        row.expires_at = now + timedelta(seconds=settings.otp_ttl_seconds)
        row.attempts, row.last_sent_at = 0, now
    db.commit()
    return email_svc.send_otp(acct.email, acct.display_name or "there", code)


class SignupIn(BaseModel):
    full_name: str
    email: str
    password: str
    phone: str | None = None
    accept_terms: bool = False
    @field_validator("email")
    @classmethod
    def _e(cls, v): return _norm(v)


@router.post("/signup")
def signup(body: SignupIn, db: Session = Depends(get_db)) -> dict:
    """Start manual signup: validate, create an UNVERIFIED account, email an OTP. No token yet."""
    problems = security.password_problems(body.password, MIN_PASSWORD_LEN)
    if problems:
        raise HTTPException(400, {"error": "weak_password",
                                  "detail": "Your password needs " + ", ".join(problems) + ".",
                                  "missing": problems})
    if not body.accept_terms:
        raise HTTPException(400, {"error": "terms_required",
                                  "detail": "Please accept the terms and conditions to continue."})
    if not (body.full_name or "").strip():
        raise HTTPException(400, {"error": "name_required", "detail": "Your full name is required."})

    existing = db.scalar(select(models.Account).where(models.Account.email == body.email))
    if existing is not None:
        prof = db.get(models.StudentProfile, existing.id)
        if prof is not None and prof.verified:
            raise HTTPException(409, {"error": "email_taken",
                                      "detail": "This email is already registered. Try signing in."})
        acct = existing                       # unverified -> let them restart signup
        acct.display_name = body.full_name.strip()
        cred = db.get(models.Credential, acct.id)
        if cred is None:
            db.add(models.Credential(account_id=acct.id, password_hash=security.hash_password(body.password)))
        else:
            cred.password_hash = security.hash_password(body.password)
    else:
        acct = models.Account(email=body.email, display_name=body.full_name.strip())
        db.add(acct); db.flush()
        db.add(models.Credential(account_id=acct.id, password_hash=security.hash_password(body.password)))

    prof = _profile(db, acct)
    prof.phone, prof.verified = body.phone, False
    meta = db.get(models.AccountAuth, acct.id)
    if meta is None:
        db.add(models.AccountAuth(account_id=acct.id, provider="password",
                                  terms_accepted_at=datetime.utcnow()))
    else:
        meta.provider, meta.terms_accepted_at = "password", datetime.utcnow()
    db.commit()

    delivery = _issue_otp(db, acct)
    return {"status": "otp_sent", "email": acct.email, "dev_mode": delivery.get("dev", False)}


class VerifyEmailIn(BaseModel):
    email: str
    code: str
    @field_validator("email")
    @classmethod
    def _e(cls, v): return _norm(v)


@router.post("/verify-email")
def verify_email(body: VerifyEmailIn, db: Session = Depends(get_db)) -> dict:
    """Check the OTP. On success: mark verified, send the welcome email, return a session token."""
    acct = db.scalar(select(models.Account).where(models.Account.email == body.email))
    row = db.get(models.EmailOtp, acct.id) if acct else None
    if acct is None or row is None:
        raise HTTPException(404, {"error": "no_pending_verification",
                                  "detail": "No verification is pending for this email."})
    now = datetime.utcnow()
    if now > row.expires_at:
        raise HTTPException(400, {"error": "otp_expired", "detail": "This code has expired. Request a new one."})
    if row.attempts >= settings.otp_max_attempts:
        raise HTTPException(429, {"error": "too_many_attempts",
                                  "detail": "Too many incorrect attempts. Request a new code."})
    if not security.verify_otp(body.code, row.code_hash):
        row.attempts += 1
        db.commit()
        raise HTTPException(400, {"error": "otp_incorrect",
                                  "attempts_left": max(0, settings.otp_max_attempts - row.attempts),
                                  "detail": "That code is incorrect."})
    prof = _profile(db, acct)
    prof.verified = True
    db.delete(row)
    db.commit()
    email_svc.send_welcome(acct.email, acct.display_name or "there")
    return _session_response(db, acct)


class EmailIn(BaseModel):
    email: str
    @field_validator("email")
    @classmethod
    def _e(cls, v): return _norm(v)


@router.post("/resend-otp")
def resend_otp(body: EmailIn, db: Session = Depends(get_db)) -> dict:
    """Re-send the OTP, subject to the 30s cooldown."""
    acct = db.scalar(select(models.Account).where(models.Account.email == body.email))
    if acct is None or db.get(models.EmailOtp, acct.id) is None:
        raise HTTPException(404, {"error": "no_pending_verification"})
    prof = db.get(models.StudentProfile, acct.id)
    if prof is not None and prof.verified:
        raise HTTPException(400, {"error": "already_verified", "detail": "This email is already verified."})
    delivery = _issue_otp(db, acct, cooldown=True)
    return {"status": "otp_sent", "email": acct.email, "dev_mode": delivery.get("dev", False)}


class ChangeEmailIn(BaseModel):
    email: str
    new_email: str
    @field_validator("email", "new_email")
    @classmethod
    def _e(cls, v): return _norm(v)


@router.post("/change-email")
def change_email(body: ChangeEmailIn, db: Session = Depends(get_db)) -> dict:
    """Change the pending (unverified) account's email and send a fresh OTP to the new address."""
    acct = db.scalar(select(models.Account).where(models.Account.email == body.email))
    if acct is None or db.get(models.EmailOtp, acct.id) is None:
        raise HTTPException(404, {"error": "no_pending_verification"})
    prof = db.get(models.StudentProfile, acct.id)
    if prof is not None and prof.verified:
        raise HTTPException(400, {"error": "already_verified", "detail": "This account is already verified."})
    if not body.new_email or "@" not in body.new_email:
        raise HTTPException(400, {"error": "invalid_email"})
    if db.scalar(select(models.Account).where(models.Account.email == body.new_email)) is not None:
        raise HTTPException(409, {"error": "email_taken", "detail": "That email is already in use."})
    acct.email = body.new_email
    db.commit()
    delivery = _issue_otp(db, acct)          # fresh code to the new address; cooldown resets
    return {"status": "otp_sent", "email": acct.email, "dev_mode": delivery.get("dev", False)}


# ----------------------------- Google sign-in (new) -----------------------------
class GoogleIn(BaseModel):
    id_token: str
    accept_terms: bool = False


@router.post("/google")
def google_signin(body: GoogleIn, db: Session = Depends(get_db)) -> dict:
    """Sign up or sign in with Google. Verifies the ID token, then links/creates the account. Google
    accounts skip OTP (Google already verified the email)."""
    info = google_auth.verify_id_token(body.id_token)
    email, sub, name = info["email"], info["sub"], info["name"]

    meta = db.scalar(select(models.AccountAuth).where(models.AccountAuth.google_sub == sub)) if sub else None
    acct = db.get(models.Account, meta.account_id) if meta else None
    if acct is None and email:
        acct = db.scalar(select(models.Account).where(models.Account.email == email))   # link by email

    if acct is None:
        if not body.accept_terms:
            raise HTTPException(400, {"error": "terms_required",
                                      "detail": "Please accept the terms and conditions to continue."})
        acct = models.Account(email=email, display_name=name)
        db.add(acct); db.flush()
        prof = models.StudentProfile(account_id=acct.id, verified=True)
        db.add(prof)
        db.add(models.AccountAuth(account_id=acct.id, provider="google", google_sub=sub,
                                  terms_accepted_at=datetime.utcnow()))
        db.commit()
        email_svc.send_welcome(acct.email, acct.display_name or "there")
    else:
        m = db.get(models.AccountAuth, acct.id)
        if m is None:
            db.add(models.AccountAuth(account_id=acct.id, provider="google", google_sub=sub,
                                      terms_accepted_at=datetime.utcnow()))
        elif not m.google_sub:
            m.google_sub = sub
        prof = _profile(db, acct)
        prof.verified = True                 # Google-verified email
        db.commit()

    _touch_last_login(db, acct)
    return _session_response(db, acct)


# ----------------------------- logout -----------------------------
@router.post("/logout")
def logout(authorization: str | None = Header(None), db: Session = Depends(get_db)) -> dict:
    """Revoke the current session token (no-op for legacy JWTs, which simply expire)."""
    if authorization and authorization.lower().startswith("bearer "):
        tok = authorization.split(" ", 1)[1].strip()
        if tok.startswith("vls_"):
            sessions.revoke(db, tok)
    return {"status": "logged_out"}
