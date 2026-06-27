"""Razorpay payments + subscriptions (Phase 15).

No network: the Razorpay order call is monkeypatched, and the two signatures are computed locally with
the same HMAC the gateway uses — so the security-critical paths (verify + webhook) are exercised exactly.
"""
import hashlib
import hmac
import json as _json
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app import models
from app.config import settings
from app.db import SessionLocal
from app.main import app
from app.services import payments


def _sig(msg: str, secret: str) -> str:
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def _buyer(c, email):
    tok = c.post("/auth/register", json={"email": email, "password": "password123",
                                         "display_name": "Buyer"}).json()["access_token"]
    return {"Authorization": "Bearer " + tok}


def test_plans_seeded_and_listed():
    with TestClient(app) as c:
        plans = c.get("/payments/plans").json()["plans"]
        codes = {p["code"] for p in plans}
        assert {"cat_1m", "cat_3m", "cat_6m", "gmat_1m", "gre_6m"} <= codes
        assert next(p for p in plans if p["code"] == "cat_6m")["months"] == 6


def test_order_only_for_subscription_plans(monkeypatch):
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_x", raising=False)
    monkeypatch.setattr(settings, "razorpay_key_secret", "sec", raising=False)
    monkeypatch.setattr(payments, "create_order", lambda amount, **k: {"id": "order_X"})
    with TestClient(app) as c:
        H = _buyer(c, "o@x.test")
        assert c.post("/payments/order", json={"plan_code": "cat_pro"}, headers=H).status_code == 404  # one_time, not a pass
        assert c.post("/payments/order", json={"plan_code": "cat_3m"}, headers=H).json()["order_id"] == "order_X"


def test_verify_grants_cat_only_and_gates_gmat(monkeypatch):
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_x", raising=False)
    monkeypatch.setattr(settings, "razorpay_key_secret", "topsecret", raising=False)
    monkeypatch.setattr(settings, "enforce_entitlements", True, raising=False)
    monkeypatch.setattr(payments, "create_order", lambda amount, **k: {"id": "order_CAT1"})
    with TestClient(app) as c:
        H = _buyer(c, "cat@x.test")
        c.post("/payments/order", json={"plan_code": "cat_3m"}, headers=H)
        # forged signature is rejected, nothing granted
        assert c.post("/payments/verify", json={"razorpay_order_id": "order_CAT1",
                      "razorpay_payment_id": "pay_1", "razorpay_signature": "nope"}, headers=H).status_code == 400
        # valid signature grants CAT for 3 months
        sig = _sig("order_CAT1|pay_1", "topsecret")
        v = c.post("/payments/verify", json={"razorpay_order_id": "order_CAT1",
                   "razorpay_payment_id": "pay_1", "razorpay_signature": sig}, headers=H).json()
        assert v["status"] == "ok" and v["granted"][0]["exam"] == "CAT" and v["months"] == 3
        # CAT unlocked, GMAT still locked
        assert c.get("/billing/access", params={"exam": "CAT"}, headers=H).json()["paid"] is True
        assert c.get("/billing/access", params={"exam": "GMAT"}, headers=H).json()["paid"] is False
        # idempotent: a second verify doesn't create a second subscription
        v2 = c.post("/payments/verify", json={"razorpay_order_id": "order_CAT1",
                    "razorpay_payment_id": "pay_1", "razorpay_signature": sig}, headers=H).json()
        assert v2.get("already_granted") is True
        assert len(c.get("/payments/my", headers=H).json()["subscriptions"]) == 1


def test_subscription_expiry_revokes_access(monkeypatch):
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_x", raising=False)
    monkeypatch.setattr(settings, "razorpay_key_secret", "sk", raising=False)
    monkeypatch.setattr(settings, "enforce_entitlements", True, raising=False)
    monkeypatch.setattr(payments, "create_order", lambda amount, **k: {"id": "order_EXP"})
    with TestClient(app) as c:
        H = _buyer(c, "exp@x.test")
        c.post("/payments/order", json={"plan_code": "cat_1m"}, headers=H)
        sig = _sig("order_EXP|pay_E", "sk")
        c.post("/payments/verify", json={"razorpay_order_id": "order_EXP",
               "razorpay_payment_id": "pay_E", "razorpay_signature": sig}, headers=H)
        assert c.get("/billing/access", params={"exam": "CAT"}, headers=H).json()["paid"] is True
        # push THIS buyer's subscription into the past (scope by account; the in-memory DB is shared across tests)
        db = SessionLocal()
        acct = db.query(models.Account).filter(models.Account.email == "exp@x.test").first()
        sub = db.query(models.Subscription).filter(models.Subscription.account_id == acct.id).first()
        sub.expires_at = datetime.utcnow() - timedelta(days=1)
        db.commit(); db.close()
        # access is now gone — the paid entitlement is treated as expired
        acc = c.get("/billing/access", params={"exam": "CAT"}, headers=H).json()
        assert acc["paid"] is False and acc["status"] == "expired"


def test_webhook_grants_and_is_idempotent(monkeypatch):
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_x", raising=False)
    monkeypatch.setattr(settings, "razorpay_key_secret", "sk2", raising=False)
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "whsec", raising=False)
    monkeypatch.setattr(settings, "enforce_entitlements", True, raising=False)
    monkeypatch.setattr(payments, "create_order", lambda amount, **k: {"id": "order_WH"})
    with TestClient(app) as c:
        H = _buyer(c, "wh@x.test")
        c.post("/payments/order", json={"plan_code": "gre_6m"}, headers=H)
        body = _json.dumps({"event": "payment.captured",
                            "payload": {"payment": {"entity": {"id": "pay_WH", "order_id": "order_WH"}}}})
        # wrong signature rejected
        assert c.post("/payments/webhook", content=body,
                      headers={"X-Razorpay-Signature": "bad"}).status_code == 400
        # correct signature grants
        sig = _sig(body, "whsec")
        assert c.post("/payments/webhook", content=body,
                      headers={"X-Razorpay-Signature": sig}).status_code == 200
        assert c.get("/billing/access", params={"exam": "GRE"}, headers=H).json()["paid"] is True
        # a duplicate webhook does not double-grant
        c.post("/payments/webhook", content=body, headers={"X-Razorpay-Signature": sig})
        assert len(c.get("/payments/my", headers=H).json()["subscriptions"]) == 1
