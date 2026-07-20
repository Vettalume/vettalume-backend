"""Razorpay payment + subscription endpoints (Phase 15).

Flow (fixed-term passes):
  GET  /payments/plans     -> month-wise plans for the checkout page (public)
  POST /payments/order     -> create a Razorpay order for a plan (student auth)   [outbound to Razorpay]
  POST /payments/verify    -> verify Checkout success signature, grant access     [belt]
  POST /payments/webhook   -> Razorpay calls this; verify + grant, idempotent     [suspenders, source of truth]
  GET  /payments/my        -> the student's own subscriptions (student auth)

Access is granted ONLY after a signature check passes (verify or webhook), never on the browser's word.
Both paths funnel through _grant_for_order, which is idempotent (PaymentOrder.granted) so a duplicate
webhook + a verify call cannot double-grant.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..deps import get_current_learner, get_db
from ..services import billing, email as email_svc, payments

router = APIRouter(prefix="/payments", tags=["payments"])


def _plan_out(p: "models.PricePlan") -> dict:
    return {"code": p.code, "exam": p.exam_code, "name": p.name, "currency": p.currency,
            "amount": p.amount_cents / 100, "amount_paise": p.amount_cents,
            "months": billing.months_for_plan(p)}


@router.get("/plans")
def list_plans(exam: str | None = None, db: Session = Depends(get_db)) -> dict:
    """Month-wise access passes for the checkout page (subscription plans only)."""
    q = select(models.PricePlan).where(models.PricePlan.active.is_(True),
                                       models.PricePlan.period == "subscription")
    if exam:
        q = q.where(models.PricePlan.exam_code == exam)
    plans = sorted(db.scalars(q).all(), key=lambda p: (p.exam_code or "", p.amount_cents))
    return {"plans": [_plan_out(p) for p in plans],
            "razorpay_key_id": settings.razorpay_key_id or None,
            "configured": payments.is_configured(),
            # True once RAZORPAY_WEBHOOK_SECRET is set on the server (the value is never exposed).
            "webhook_configured": bool(settings.razorpay_webhook_secret)}


class OrderIn(BaseModel):
    plan_code: str
    coupon: str | None = None


@router.post("/order")
def create_order(body: OrderIn, learner=Depends(get_current_learner),
                 db: Session = Depends(get_db)) -> dict:
    """Create a Razorpay order for a plan (optionally applying a coupon); the browser opens Checkout
    with the returned order_id. A coupon that zeroes the price grants the plan directly (no charge)."""
    plan = db.get(models.PricePlan, body.plan_code)
    if plan is None or not plan.active or plan.period != "subscription":
        raise HTTPException(404, f"unknown plan '{body.plan_code}'")

    amount = plan.amount_cents
    coupon_code = None
    coupon_out = None
    if body.coupon:
        res = billing.validate_coupon(db, body.coupon, exam=plan.exam_code, amount=plan.amount_cents)
        if not res.get("valid"):
            raise HTTPException(400, {"error": "coupon_invalid", "detail": res.get("reason")})
        amount = int(res["final"])
        coupon_code = res["code"]
        coupon_out = {"code": res["code"], "discount": res["discount"]}

    # 100%-off (or more) coupon -> nothing to charge; grant the plan straight away and count the coupon.
    if amount <= 0:
        grant = billing.grant_subscription(db, learner, plan)
        if coupon_code:
            _consume_coupon(db, coupon_code)
        return {"free": True, "plan": _plan_out(plan), "coupon": coupon_out, **grant}

    rp = payments.create_order(
        amount, currency=plan.currency or "INR",
        receipt=f"vl_{plan.code}_{uuid.uuid4().hex[:10]}"[:40],   # Razorpay caps receipt at 40 chars
        notes={"account_id": str(learner.id), "plan_code": plan.code})  # the real linkage lives in notes
    db.add(models.PaymentOrder(id=rp["id"], account_id=learner.id, plan_code=plan.code,
                               amount_paise=amount, currency=plan.currency or "INR",
                               status="created", coupon_code=coupon_code))
    db.commit()
    return {"order_id": rp["id"], "amount": amount, "currency": plan.currency or "INR",
            "key_id": settings.razorpay_key_id, "plan": _plan_out(plan), "coupon": coupon_out,
            "name": learner.display_name, "email": learner.email}


def _consume_coupon(db: Session, code: str) -> None:
    """Increment a coupon's usage counter once a plan is actually granted."""
    c = db.scalar(select(models.Coupon).where(func.upper(models.Coupon.code) == (code or "").upper()))
    if c is not None:
        c.used = (c.used or 0) + 1
        db.commit()


class VerifyIn(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


@router.post("/verify")
def verify(body: VerifyIn, learner=Depends(get_current_learner),
           db: Session = Depends(get_db)) -> dict:
    """Belt: verify the Checkout success signature, then grant. (The webhook is the suspenders.)"""
    if not payments.verify_payment_signature(
            body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature):
        raise HTTPException(400, {"error": "signature_invalid"})
    po = db.get(models.PaymentOrder, body.razorpay_order_id)
    if po is None or po.account_id != learner.id:
        raise HTTPException(404, "unknown order")
    return {"status": "ok", **_grant_for_order(db, po, body.razorpay_payment_id)}


@router.post("/webhook")
async def webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    """Razorpay webhook — the source of truth. Verify the signature over the EXACT raw body, then
    grant idempotently. In the dashboard: add this URL, set the secret, subscribe to payment.captured."""
    raw = await request.body()
    sig = request.headers.get("X-Razorpay-Signature", "")
    if not payments.verify_webhook_signature(raw, sig):
        raise HTTPException(400, {"error": "webhook_signature_invalid"})
    event = json.loads(raw or b"{}")
    payload = event.get("payload") or {}
    payment = ((payload.get("payment") or {}).get("entity")) or {}
    order = ((payload.get("order") or {}).get("entity")) or {}
    order_id = payment.get("order_id") or order.get("id")
    if event.get("event") in ("payment.captured", "order.paid") and order_id:
        po = db.get(models.PaymentOrder, order_id)
        if po is not None:
            _grant_for_order(db, po, payment.get("id"))
    return {"status": "ok"}   # always 200 so Razorpay stops retrying


def _grant_for_order(db: Session, po: "models.PaymentOrder", payment_id: str | None) -> dict:
    """Mark the order paid and grant its plan's subscription. Idempotent via po.granted, so a
    duplicate webhook (or webhook + verify) cannot grant twice."""
    if po.granted:
        return {"already_granted": True, "order_id": po.id}
    plan = db.get(models.PricePlan, po.plan_code)
    account = db.get(models.Account, po.account_id)
    if plan is None or account is None:
        raise HTTPException(404, "order references a missing plan/account")
    po.status, po.rp_payment_id, po.granted = "paid", payment_id, True
    db.commit()
    if po.coupon_code:                       # count the coupon once, on the (idempotent) grant
        _consume_coupon(db, po.coupon_code)
    grant = billing.grant_subscription(db, account, plan, order_id=po.id, rp_payment_id=payment_id)
    # Best-effort receipt — a mail outage must not undo a captured payment (see email._best_effort).
    email_svc.send_payment_confirmation(
        account.email, account.display_name or "there",
        plan_name=plan.name, amount=po.amount_paise / 100, currency=po.currency,
        months=grant.get("months", 0), access=grant.get("granted", []), payment_id=payment_id)
    return {"order_id": po.id, **grant}


@router.get("/my")
def my_subscriptions(learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    subs = db.scalars(select(models.Subscription).where(
        models.Subscription.account_id == learner.id
    ).order_by(models.Subscription.expires_at.desc())).all()
    now = datetime.utcnow()
    return {"subscriptions": [
        {"exam": s.exam_code, "plan": s.plan_code, "expires_at": s.expires_at.isoformat(),
         "active": s.status == "active" and s.expires_at > now} for s in subs]}
