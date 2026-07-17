from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Response
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


def _set_session_cookie(response: Response, token: str) -> None:
    """Set the session token as a Secure, HttpOnly cookie — but only when SESSION_COOKIE_DOMAIN is
    configured (a shared parent domain). While it's empty (default) this is a no-op and auth stays
    header-only, so today's cross-domain setup is unaffected."""
    domain = settings.session_cookie_domain.strip()
    if not domain:
        return
    response.set_cookie(
        key=settings.session_cookie_name, value=token,
        max_age=settings.session_max_days * 86400,
        httponly=True, secure=True, samesite="lax", domain=domain, path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    domain = settings.session_cookie_domain.strip()
    if domain:
        response.delete_cookie(settings.session_cookie_name, domain=domain, path="/")


def _session_response(db: Session, acct: models.Account, response: Response | None = None) -> dict:
    """New sliding-session response: an opaque, device-bound token (valid until 24h idle / 7-day cap).
    Also sets it as an HttpOnly cookie when cookie auth is enabled (SESSION_COOKIE_DOMAIN)."""
    token = sessions.create(db, acct)
    if response is not None:
        _set_session_cookie(response, token)
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


def _record_login_failure(db: Session, account_id, now: datetime) -> None:
    """Count a wrong-password attempt for a known account; lock it once the limit is hit."""
    t = db.get(models.LoginThrottle, account_id)
    if t is None:
        t = models.LoginThrottle(account_id=account_id, fail_count=0)
        db.add(t)
    t.fail_count = (t.fail_count or 0) + 1
    t.updated_at = now
    if t.fail_count >= settings.login_max_attempts:
        t.locked_until = now + timedelta(minutes=settings.login_lockout_minutes)
        t.fail_count = 0   # the lockout window is the penalty; start fresh after it passes
    db.commit()


@router.post("/login")
def login(body: LoginIn, response: Response, db: Session = Depends(get_db)) -> dict:
    """Email + password sign-in. Returns a device-bound sliding session token. New-flow accounts must
    have verified their email first; legacy accounts (no auth metadata) are grandfathered in. Repeated
    wrong passwords temporarily lock the account (brute-force defence)."""
    acct = db.scalar(select(models.Account).where(models.Account.email == body.email))
    cred = db.get(models.Credential, acct.id) if acct else None
    now = datetime.utcnow()

    # Brute-force lockout (only meaningful for a real account; unknown emails hit the generic 401).
    throttle = db.get(models.LoginThrottle, acct.id) if acct else None
    if throttle is not None and throttle.locked_until is not None and now < throttle.locked_until:
        mins = int((throttle.locked_until - now).total_seconds() // 60) + 1
        raise HTTPException(429, {"error": "account_locked",
                                  "detail": f"Too many failed attempts. Try again in about {mins} minute(s)."})

    if acct is None or cred is None or not security.verify_password(body.password, cred.password_hash):
        if acct is not None:
            _record_login_failure(db, acct.id, now)
        raise HTTPException(401, "invalid email or password")   # one message -> no account enumeration

    meta = db.get(models.AccountAuth, acct.id)
    prof = db.get(models.StudentProfile, acct.id)
    if meta is not None and meta.provider == "password" and prof is not None and not prof.verified:
        raise HTTPException(403, {"error": "email_not_verified",
                                  "detail": "Please verify your email before signing in."})
    if throttle is not None and (throttle.fail_count or throttle.locked_until):
        throttle.fail_count, throttle.locked_until, throttle.updated_at = 0, None, now  # clear on success
        db.commit()
    _touch_last_login(db, acct)
    return _session_response(db, acct, response)


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
    delivery = email_svc.send_otp(acct.email, acct.display_name or "there", code)
    # In dev the email isn't actually sent (services.email.send_email short-circuits), so surface the
    # code back to the client so the signup UI can show it — no mail provider or DNS needed. dev_mode
    # is force-disabled in production by production_problems(), so this never leaks a real OTP live.
    if settings.dev_mode and isinstance(delivery, dict):
        delivery = {**delivery, "code": code}
    return delivery


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
    return {"status": "otp_sent", "email": acct.email, "dev_mode": delivery.get("dev", False),
            "otp": delivery.get("code")}


class VerifyEmailIn(BaseModel):
    email: str
    code: str
    @field_validator("email")
    @classmethod
    def _e(cls, v): return _norm(v)


@router.post("/verify-email")
def verify_email(body: VerifyEmailIn, response: Response, db: Session = Depends(get_db)) -> dict:
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
    return _session_response(db, acct, response)


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
    return {"status": "otp_sent", "email": acct.email, "dev_mode": delivery.get("dev", False),
            "otp": delivery.get("code")}


# ----------------------------- forgot / reset password (new) -----------------------------
class ForgotPasswordIn(BaseModel):
    email: str
    @field_validator("email")
    @classmethod
    def _e(cls, v): return _norm(v)


@router.post("/forgot-password")
def forgot_password(body: ForgotPasswordIn, db: Session = Depends(get_db)) -> dict:
    """Start a password reset: email an OTP to the address. Always returns ok (no account enumeration);
    an OTP is only actually sent when the email belongs to a password account."""
    acct = db.scalar(select(models.Account).where(models.Account.email == body.email))
    dev, code = False, None
    if acct is not None and db.get(models.Credential, acct.id) is not None:
        delivery = _issue_otp(db, acct)
        dev, code = delivery.get("dev", False), delivery.get("code")
    return {"status": "otp_sent", "email": body.email, "dev_mode": dev, "otp": code}


class ResetPasswordIn(BaseModel):
    email: str
    code: str
    new_password: str
    @field_validator("email")
    @classmethod
    def _e(cls, v): return _norm(v)


@router.post("/reset-password")
def reset_password(body: ResetPasswordIn, response: Response, db: Session = Depends(get_db)) -> dict:
    """Verify the reset OTP and set a new password. On success returns a fresh session token."""
    problems = security.password_problems(body.new_password, MIN_PASSWORD_LEN)
    if problems:
        raise HTTPException(400, {"error": "weak_password",
                                  "detail": "Your password needs " + ", ".join(problems) + ".",
                                  "missing": problems})
    acct = db.scalar(select(models.Account).where(models.Account.email == body.email))
    row = db.get(models.EmailOtp, acct.id) if acct else None
    if acct is None or row is None:
        raise HTTPException(404, {"error": "no_pending_reset",
                                  "detail": "No reset is pending for this email. Request a new code."})
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
    cred = db.get(models.Credential, acct.id)
    if cred is None:
        db.add(models.Credential(account_id=acct.id, password_hash=security.hash_password(body.new_password)))
    else:
        cred.password_hash = security.hash_password(body.new_password)
    db.delete(row)
    db.commit()
    return _session_response(db, acct, response)


# ----------------------------- self-service profile (new) -----------------------------
def _profile_out(learner: "models.Account", p: "models.StudentProfile") -> dict:
    return {"email": learner.email, "full_name": learner.display_name or "",
            "phone": p.phone or "", "city": p.city or "",
            "about": p.about or "", "target_exam": p.target_exam or ""}


@router.get("/profile")
def get_profile(learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    """The signed-in student's own editable profile."""
    p = _profile(db, learner)
    db.commit()
    return _profile_out(learner, p)


class ProfileUpdateIn(BaseModel):
    full_name: str | None = None
    phone: str | None = None
    city: str | None = None
    about: str | None = None
    target_exam: str | None = None


@router.patch("/profile")
def update_profile(body: ProfileUpdateIn, learner=Depends(get_current_learner),
                   db: Session = Depends(get_db)) -> dict:
    """Save the signed-in student's profile fields (persists to the accounts / student_profiles tables)."""
    if body.full_name is not None and body.full_name.strip():
        learner.display_name = body.full_name.strip()
    p = _profile(db, learner)
    if body.phone is not None:
        p.phone = body.phone.strip() or None
    if body.city is not None:
        p.city = body.city.strip() or None
    if body.about is not None:
        p.about = body.about.strip() or None
    if body.target_exam is not None:
        p.target_exam = body.target_exam.strip() or None
    db.commit()
    return {"status": "saved", **_profile_out(learner, p)}


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
def google_signin(body: GoogleIn, response: Response, db: Session = Depends(get_db)) -> dict:
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
    return _session_response(db, acct, response)


# ----------------------------- change password -----------------------------
class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


@router.post("/change-password")
def change_password(body: ChangePasswordIn, authorization: str | None = Header(None),
                    learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    """Change the signed-in account's password. Verifies the current password, enforces strength on the
    new one, re-hashes, then revokes all OTHER sessions so a stolen/old token can't outlive the change."""
    cred = db.get(models.Credential, learner.id)
    if cred is None:
        raise HTTPException(400, {"error": "no_password_set",
                                  "detail": "This account has no password (e.g. Google sign-in)."})
    if not security.verify_password(body.current_password, cred.password_hash):
        raise HTTPException(403, {"error": "wrong_password", "detail": "Your current password is incorrect."})
    problems = security.password_problems(body.new_password, MIN_PASSWORD_LEN)
    if problems:
        raise HTTPException(400, {"error": "weak_password",
                                  "detail": "Your new password needs " + ", ".join(problems) + ".",
                                  "missing": problems})
    cred.password_hash = security.hash_password(body.new_password)
    db.commit()
    keep = None
    if authorization and authorization.lower().startswith("bearer "):
        tok = authorization.split(" ", 1)[1].strip()
        if tok.startswith("vls_"):
            keep = tok
    revoked = sessions.revoke_all(db, learner.id, keep_token=keep)
    return {"status": "password_changed", "other_sessions_revoked": revoked}


# ----------------------------- logout -----------------------------
@router.post("/logout")
def logout(response: Response, authorization: str | None = Header(None),
           vls_session: str | None = Cookie(None), db: Session = Depends(get_db)) -> dict:
    """Revoke the current session token (from header or cookie) and clear the session cookie.
    No-op for legacy JWTs, which simply expire."""
    tok = None
    if authorization and authorization.lower().startswith("bearer "):
        tok = authorization.split(" ", 1)[1].strip()
    elif vls_session:
        tok = vls_session.strip()
    if tok and tok.startswith("vls_"):
        sessions.revoke(db, tok)
    _clear_session_cookie(response)
    return {"status": "logged_out"}


@router.post("/logout-all")
def logout_all(authorization: str | None = Header(None),
               learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    """Revoke every session for the current account ("log out of all devices"). Keeps the caller's own
    session alive so this request's token stays usable. Use after a password change or suspected compromise."""
    keep = None
    if authorization and authorization.lower().startswith("bearer "):
        tok = authorization.split(" ", 1)[1].strip()
        if tok.startswith("vls_"):
            keep = tok
    revoked = sessions.revoke_all(db, learner.id, keep_token=keep)
    return {"status": "logged_out_all", "sessions_revoked": revoked}
