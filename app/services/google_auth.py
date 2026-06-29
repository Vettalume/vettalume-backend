"""Google sign-in token verification (Phase 16).

The frontend's Google button yields an ID token; we verify it via Google's tokeninfo endpoint (Google
validates the signature/expiry), then confirm the audience is OUR client id. Returns the identity.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from fastapi import HTTPException

from ..config import settings

_TOKENINFO = "https://oauth2.googleapis.com/tokeninfo"


def is_configured() -> bool:
    return bool(settings.google_client_id)


def verify_id_token(id_token: str) -> dict:
    """Return {email, name, sub, email_verified} for a valid Google ID token; raise otherwise."""
    if not is_configured():
        raise HTTPException(503, {"error": "google_not_configured",
                                  "detail": "Set GOOGLE_CLIENT_ID to enable Google sign-in."})
    if not id_token:
        raise HTTPException(400, "missing id_token")
    url = f"{_TOKENINFO}?{urllib.parse.urlencode({'id_token': id_token})}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            info = json.loads(r.read())
    except urllib.error.HTTPError:
        raise HTTPException(401, {"error": "google_token_invalid"})
    except urllib.error.URLError as e:
        raise HTTPException(502, {"error": "google_unreachable", "detail": str(e.reason)})
    if info.get("aud") != settings.google_client_id:
        raise HTTPException(401, {"error": "google_audience_mismatch"})
    if info.get("iss") not in ("https://accounts.google.com", "accounts.google.com"):
        raise HTTPException(401, {"error": "google_issuer_invalid"})
    email = (info.get("email") or "").lower()
    return {"email": email, "sub": info.get("sub"),
            "name": info.get("name") or (email.split("@")[0] if email else "there"),
            "email_verified": str(info.get("email_verified")).lower() == "true"}
