"""Student auth (Phase 16): signup -> email OTP -> verify -> login, plus Google sign-in and the
2-day sliding session. Email and Google verification are monkeypatched, so no network or mail is hit;
the OTP is captured from the (patched) send call so the verify step can use the real code.
"""
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app import models
from app.config import settings
from app.db import SessionLocal
from app.main import app
from app.services import email as email_svc
from app.services import google_auth


def _capture(monkeypatch):
    box = {}
    def fake_otp(to, name, code):
        box["code"], box["to"] = code, to
        return {"sent": False, "dev": True}
    monkeypatch.setattr(email_svc, "send_otp", fake_otp)
    monkeypatch.setattr(email_svc, "send_welcome", lambda *a, **k: {"sent": False, "dev": True})
    return box


def test_signup_rejects_weak_password(monkeypatch):
    _capture(monkeypatch)
    with TestClient(app) as c:
        r = c.post("/auth/signup", json={"full_name": "A B", "email": "weak@s.test",
                   "password": "alllowercase", "accept_terms": True})
        assert r.status_code == 400 and r.json()["detail"]["error"] == "weak_password"


def test_signup_requires_terms(monkeypatch):
    _capture(monkeypatch)
    with TestClient(app) as c:
        r = c.post("/auth/signup", json={"full_name": "A B", "email": "not@s.test",
                   "password": "Strong@123", "accept_terms": False})
        assert r.status_code == 400 and r.json()["detail"]["error"] == "terms_required"


def test_signup_verify_login_flow(monkeypatch):
    box = _capture(monkeypatch)
    with TestClient(app) as c:
        r = c.post("/auth/signup", json={"full_name": "Abhishek K", "email": "stud@s.test",
                   "password": "Strong@123", "phone": "+91 90000 00000", "accept_terms": True})
        assert r.json()["status"] == "otp_sent"
        # wrong code is rejected with attempts_left
        bad = c.post("/auth/verify-email", json={"email": "stud@s.test", "code": "000000"})
        assert bad.status_code == 400 and bad.json()["detail"]["error"] == "otp_incorrect"
        # correct code -> a sliding-session token
        ok = c.post("/auth/verify-email", json={"email": "stud@s.test", "code": box["code"]}).json()
        assert ok["access_token"].startswith("vls_")
        H = {"Authorization": "Bearer " + ok["access_token"]}
        assert c.get("/auth/me", headers=H).json()["email"] == "stud@s.test"
        # phone + verified persisted
        db = SessionLocal()
        acct = db.query(models.Account).filter(models.Account.email == "stud@s.test").first()
        prof = db.get(models.StudentProfile, acct.id)
        assert prof.verified is True and prof.phone == "+91 90000 00000"
        db.close()
        # now email+password login works and also returns a session token
        lg = c.post("/auth/login", json={"email": "stud@s.test", "password": "Strong@123"}).json()
        assert lg["access_token"].startswith("vls_")


def test_login_blocked_until_verified(monkeypatch):
    _capture(monkeypatch)
    with TestClient(app) as c:
        c.post("/auth/signup", json={"full_name": "Unv", "email": "unv@s.test",
               "password": "Strong@123", "accept_terms": True})
        r = c.post("/auth/login", json={"email": "unv@s.test", "password": "Strong@123"})
        assert r.status_code == 403 and r.json()["detail"]["error"] == "email_not_verified"


def test_resend_cooldown(monkeypatch):
    _capture(monkeypatch)
    with TestClient(app) as c:
        c.post("/auth/signup", json={"full_name": "RC", "email": "rc@s.test",
               "password": "Strong@123", "accept_terms": True})
        r = c.post("/auth/resend-otp", json={"email": "rc@s.test"})   # immediate -> still in cooldown
        assert r.status_code == 429 and r.json()["detail"]["error"] == "resend_cooldown"
        assert r.json()["detail"]["retry_after"] >= 1


def test_change_email(monkeypatch):
    box = _capture(monkeypatch)
    with TestClient(app) as c:
        c.post("/auth/signup", json={"full_name": "CE", "email": "old@s.test",
               "password": "Strong@123", "accept_terms": True})
        r = c.post("/auth/change-email", json={"email": "old@s.test", "new_email": "new@s.test"}).json()
        assert r["email"] == "new@s.test"
        ok = c.post("/auth/verify-email", json={"email": "new@s.test", "code": box["code"]})
        assert ok.status_code == 200 and ok.json()["access_token"].startswith("vls_")


def test_google_signup_and_relogin(monkeypatch):
    monkeypatch.setattr(email_svc, "send_welcome", lambda *a, **k: {"dev": True})
    monkeypatch.setattr(google_auth, "verify_id_token",
                        lambda tok: {"email": "g@s.test", "sub": "gsub-1", "name": "Goo Gle", "email_verified": True})
    with TestClient(app) as c:
        assert c.post("/auth/google", json={"id_token": "x", "accept_terms": False}).status_code == 400  # terms
        r = c.post("/auth/google", json={"id_token": "x", "accept_terms": True}).json()
        assert r["access_token"].startswith("vls_") and r["account"]["email"] == "g@s.test"
        r2 = c.post("/auth/google", json={"id_token": "x"}).json()       # second time, no terms needed
        assert r2["account"]["id"] == r["account"]["id"]


def test_session_sliding_expiry_and_logout(monkeypatch):
    monkeypatch.setattr(email_svc, "send_welcome", lambda *a, **k: {"dev": True})
    monkeypatch.setattr(google_auth, "verify_id_token",
                        lambda tok: {"email": "exp@s.test", "sub": "gsub-exp", "name": "Exp", "email_verified": True})
    with TestClient(app) as c:
        tok = c.post("/auth/google", json={"id_token": "x", "accept_terms": True}).json()["access_token"]
        H = {"Authorization": "Bearer " + tok}
        assert c.get("/auth/me", headers=H).status_code == 200
        # 3 idle days -> auto-logout
        db = SessionLocal()
        s = db.get(models.AuthSession, tok)
        s.last_seen_at = datetime.utcnow() - timedelta(days=3)
        db.commit(); db.close()
        assert c.get("/auth/me", headers=H).status_code == 401
        # a fresh session, then explicit logout revokes it
        tok2 = c.post("/auth/google", json={"id_token": "x"}).json()["access_token"]
        H2 = {"Authorization": "Bearer " + tok2}
        assert c.get("/auth/me", headers=H2).status_code == 200
        c.post("/auth/logout", headers=H2)
        assert c.get("/auth/me", headers=H2).status_code == 401
