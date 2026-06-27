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
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..deps import get_current_learner, get_db
from ..services import billing, payments

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
            "configured": payments.is_configured()}


class OrderIn(BaseModel):
    plan_code: str


@router.post("/order")
def create_order(body: OrderIn, learner=Depends(get_current_learner),
                 db: Session = Depends(get_db)) -> dict:
    """Create a Razorpay order for a plan; the browser opens Checkout with the returned order_id."""
    plan = db.get(models.PricePlan, body.plan_code)
    if plan is None or not plan.active or plan.period != "subscription":
        raise HTTPException(404, f"unknown plan '{body.plan_code}'")
    rp = payments.create_order(
        plan.amount_cents, currency=plan.currency or "INR",
        receipt=f"vl_{plan.code}_{uuid.uuid4().hex[:10]}"[:40],   # Razorpay caps receipt at 40 chars
        notes={"account_id": str(learner.id), "plan_code": plan.code})  # the real linkage lives in notes
    db.add(models.PaymentOrder(id=rp["id"], account_id=learner.id, plan_code=plan.code,
                               amount_paise=plan.amount_cents, currency=plan.currency or "INR",
                               status="created"))
    db.commit()
    return {"order_id": rp["id"], "amount": plan.amount_cents, "currency": plan.currency or "INR",
            "key_id": settings.razorpay_key_id, "plan": _plan_out(plan),
            "name": learner.display_name, "email": learner.email}


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
    grant = billing.grant_subscription(db, account, plan, order_id=po.id, rp_payment_id=payment_id)
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
