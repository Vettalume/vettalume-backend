"""Razorpay client (Phase 15).

Pure gateway adapter — no database. Two responsibilities:
  1. Create an order on Razorpay (the only outbound HTTP call).
  2. Verify the two signatures Razorpay sends, which is the entire security model:
       - payment signature  = HMAC_SHA256(order_id + "|" + payment_id, key_secret)
       - webhook signature  = HMAC_SHA256(raw_request_body,            webhook_secret)

Both are compared in constant time. Access is NEVER granted on the browser's word; it is granted
only after one of these checks passes. Keys come from settings (env), never the frontend.

Implemented with the stdlib (urllib + hmac) so there is no extra dependency and the security-critical
parts are unit-testable without network. If you prefer the official SDK, `pip install razorpay` and
swap create_order(); the signature helpers are identical to razorpay.Utility.verify_*.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.error
import urllib.request

from fastapi import HTTPException

from ..config import settings

_API = "https://api.razorpay.com/v1"


def is_configured() -> bool:
    return bool(settings.razorpay_key_id and settings.razorpay_key_secret)


def _require_configured() -> None:
    if not is_configured():
        raise HTTPException(503, {
            "error": "razorpay_not_configured",
            "detail": "Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET (use rzp_test_* keys for now).",
        })


def create_order(amount_paise: int, currency: str = "INR", receipt: str | None = None,
                 notes: dict | None = None) -> dict:
    """Create a Razorpay order. Returns Razorpay's order object (its `id` is order_...)."""
    _require_configured()
    payload = json.dumps({
        "amount": int(amount_paise), "currency": currency,
        "receipt": receipt or "", "notes": notes or {},
        "payment_capture": 1,   # auto-capture on success
    }).encode()
    auth = base64.b64encode(
        f"{settings.razorpay_key_id}:{settings.razorpay_key_secret}".encode()).decode()
    req = urllib.request.Request(f"{_API}/orders", data=payload, method="POST", headers={
        "Authorization": f"Basic {auth}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:        # Razorpay rejected the request
        raise HTTPException(502, {"error": "razorpay_order_failed", "detail": e.read().decode()[:300]})
    except urllib.error.URLError as e:         # couldn't reach Razorpay
        raise HTTPException(502, {"error": "razorpay_unreachable", "detail": str(e.reason)})


def verify_payment_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """Razorpay Checkout success callback signature."""
    if not (order_id and payment_id and signature and settings.razorpay_key_secret):
        return False
    expected = hmac.new(settings.razorpay_key_secret.encode(),
                        f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """Razorpay webhook signature (X-Razorpay-Signature header over the exact raw body)."""
    secret = settings.razorpay_webhook_secret
    if not (raw_body and signature and secret):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
