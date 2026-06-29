"""Security primitives (Phase 6) — password hashing and JWTs, stdlib only (no new dependencies).

Passwords use PBKDF2-HMAC-SHA256 with a random per-user salt; tokens are HS256 JWTs. Both are FIPS-
standard constructions and are implemented with `hmac.compare_digest` (constant-time) and a hard-pinned
algorithm (alg=none / RS256<->HS256 confusion is rejected). For production you would swap PBKDF2 for
argon2id and this hand-rolled JWT for PyJWT — both are localized to this module so the swap is one file.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from ..config import settings

_PBKDF2_ITERS = 200_000


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---------------- passwords ----------------
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERS)
    return f"pbkdf2_sha256${_PBKDF2_ITERS}${_b64u_encode(salt)}${_b64u_encode(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 _b64u_decode(salt_b64), int(iters))
        return hmac.compare_digest(dk, _b64u_decode(hash_b64))
    except Exception:
        return False


# ---------------- JWT (HS256) ----------------
def _sign(signing_input: bytes) -> bytes:
    return hmac.new(settings.jwt_secret.encode("utf-8"), signing_input, hashlib.sha256).digest()


def make_token(sub, *, expires_in: int | None = None, extra: dict | None = None) -> str:
    now = int(time.time())
    payload = {"sub": str(sub), "iat": now,
               "exp": now + (expires_in if expires_in is not None else settings.jwt_expiry_seconds)}
    if extra:
        payload.update(extra)
    header = {"alg": "HS256", "typ": "JWT"}
    seg = (_b64u_encode(json.dumps(header, separators=(",", ":")).encode())
           + "." + _b64u_encode(json.dumps(payload, separators=(",", ":")).encode()))
    return seg + "." + _b64u_encode(_sign(seg.encode("ascii")))


def decode_token(token: str) -> dict:
    """Verify and decode an HS256 JWT. Raises ValueError on any problem (bad alg, bad signature,
    expired, malformed). The algorithm is pinned so 'none' and RS/HS confusion are rejected."""
    try:
        h_b, p_b, s_b = token.split(".")
    except ValueError:
        raise ValueError("malformed token")
    try:
        header = json.loads(_b64u_decode(h_b))
    except Exception:
        raise ValueError("malformed header")
    if header.get("alg") != "HS256":
        raise ValueError("unexpected algorithm")
    expected = _b64u_encode(_sign(f"{h_b}.{p_b}".encode("ascii")))
    if not hmac.compare_digest(expected, s_b):
        raise ValueError("bad signature")
    payload = json.loads(_b64u_decode(p_b))
    if int(payload.get("exp", 0)) < int(time.time()):
        raise ValueError("expired")
    return payload


# ---------- password strength ----------
import re as _re
import secrets as _secrets

_PW_SPECIAL = _re.compile(r"[^A-Za-z0-9]")


def password_problems(pw: str, min_len: int = 8) -> list[str]:
    """Return a list of unmet requirements (empty list == strong enough). Used to give the user
    specific, actionable feedback rather than a vague 'weak password'."""
    problems = []
    if len(pw or "") < min_len:
        problems.append(f"at least {min_len} characters")
    if not _re.search(r"[A-Z]", pw or ""):
        problems.append("an uppercase letter")
    if not _re.search(r"[a-z]", pw or ""):
        problems.append("a lowercase letter")
    if not _re.search(r"[0-9]", pw or ""):
        problems.append("a number")
    if not _PW_SPECIAL.search(pw or ""):
        problems.append("a special character")
    return problems


# ---------- opaque session tokens ----------
def new_session_token() -> str:
    """A high-entropy, URL-safe opaque token. This IS the AuthSession primary key / bearer value."""
    return "vls_" + _secrets.token_urlsafe(36)


# ---------- email OTP (short-lived, rate-limited; stored hashed, never plaintext) ----------
def new_otp(digits: int = 6) -> str:
    return "".join(_secrets.choice("0123456789") for _ in range(digits))


def hash_otp(code: str) -> str:
    """Keyed hash of an OTP for at-rest storage. HMAC (not PBKDF2) is fine here: codes are short-lived,
    attempt-limited, and high-enough entropy that brute force in the window is infeasible."""
    return hmac.new(settings.jwt_secret.encode("utf-8"), (code or "").encode("utf-8"),
                    hashlib.sha256).hexdigest()


def verify_otp(code: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_otp(code), stored_hash or "")
