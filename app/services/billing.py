"""Billing and entitlements (Phase 5).

One account, one wallet, independently-purchasable per-course entitlements, multi-exam bundles with a
cross-course discount, and per-course free tiers (BL-01..05, AC-01). This is the records-and-logic
layer: purchase() records an Order and grants Entitlements but does NOT move money — a real payment
provider (Stripe for USD, Razorpay for INR) slots in at the marked point.

Entitlement tier is encoded in Entitlement.status: free | active (paid) | expired. Enforcement is
gated behind settings.enforce_entitlements so the open demo keeps working until it is switched on.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import settings

# One coherent catalog. Prices are illustrative; the point is multi-currency + tiers + a bundle.
_CATALOG = [
    dict(code="gmat_free", kind="free", exam_code="GMAT", name="GMAT Free",
         currency="USD", amount_cents=0, limits={"full_mocks": 1, "debrief": "summary"}),
    dict(code="gmat_summit", kind="paid", exam_code="GMAT", name="GMAT Summit",
         currency="USD", amount_cents=19900, limits=None),
    dict(code="gre_free", kind="free", exam_code="GRE", name="GRE Free",
         currency="USD", amount_cents=0, limits={"full_mocks": 2, "debrief": "summary"}),
    dict(code="gre_core", kind="paid", exam_code="GRE", name="GRE Core",
         currency="USD", amount_cents=14900, limits=None),
    dict(code="cat_free", kind="free", exam_code="CAT", name="CAT Free",
         currency="INR", amount_cents=0, limits={"full_mocks": 1, "debrief": "summary"}),
    dict(code="cat_pro", kind="paid", exam_code="CAT", name="CAT Pro",
         currency="INR", amount_cents=1499900, limits=None),
    dict(code="bundle_gmat_gre", kind="bundle", exam_code=None, name="GMAT + GRE Bundle",
         currency="USD", amount_cents=29900, bundle_exams=["GMAT", "GRE"], limits=None),
]

# Fixed-term access passes shown on the checkout page. (code, exam, display name, paise, months).
# Prices are paise (₹999 = 99900). Tune freely — they're seeded only if missing.
_SUBSCRIPTION_PLANS = [
    ("cat_1m",  "CAT",  "CAT — 1 Month",   99900, 1),
    ("cat_3m",  "CAT",  "CAT — 3 Months",  249900, 3),
    ("cat_6m",  "CAT",  "CAT — 6 Months",  399900, 6),
    ("gmat_1m", "GMAT", "GMAT — 1 Month",  129900, 1),
    ("gmat_3m", "GMAT", "GMAT — 3 Months", 299900, 3),
    ("gmat_6m", "GMAT", "GMAT — 6 Months", 499900, 6),
    ("gre_1m",  "GRE",  "GRE — 1 Month",   119900, 1),
    ("gre_3m",  "GRE",  "GRE — 3 Months",  279900, 3),
    ("gre_6m",  "GRE",  "GRE — 6 Months",  459900, 6),
]


def ensure_catalog(db: Session) -> None:
    """Idempotently load the SKU catalog (safe to call on every boot)."""
    changed = False
    for spec in _CATALOG:
        if db.get(models.PricePlan, spec["code"]) is None:
            db.add(models.PricePlan(active=True, period="one_time", **spec))
            changed = True
    # Month-wise access passes (fixed-term). Duration lives in limits.months; all INR (India audience).
    for code, exam, name, paise, months in _SUBSCRIPTION_PLANS:
        if db.get(models.PricePlan, code) is None:
            db.add(models.PricePlan(code=code, kind="paid", exam_code=exam, name=name, currency="INR",
                                    amount_cents=paise, period="subscription", active=True,
                                    limits={"months": months}))
            changed = True
    if changed:
        db.commit()


def catalog(db: Session) -> list[dict]:
    rows = db.scalars(select(models.PricePlan).where(models.PricePlan.active.is_(True))).all()
    return [{"code": p.code, "kind": p.kind, "exam": p.exam_code, "name": p.name,
             "currency": p.currency, "amount": p.amount_cents / 100, "amount_cents": p.amount_cents,
             "period": p.period, "bundle_exams": p.bundle_exams, "limits": p.limits} for p in rows]


def _entitlement(db: Session, account: models.Account, exam: str) -> models.Entitlement | None:
    return db.scalar(select(models.Entitlement).where(
        models.Entitlement.account_id == account.id, models.Entitlement.exam_code == exam))


def entitlement_state(db: Session, account: models.Account, exam: str) -> dict:
    ent = _entitlement(db, account, exam)
    status = ent.status if ent else None
    if status == "active":
        # A subscription-backed entitlement is valid only while a subscription is live. An admin
        # manual grant (no Subscription row at all) is permanent and is left untouched.
        has_sub = db.scalar(select(models.Subscription.id).where(
            models.Subscription.account_id == account.id,
            models.Subscription.exam_code == exam).limit(1))
        if has_sub is not None and active_subscription(db, account, exam) is None:
            status = "expired"
    tier = "paid" if status == "active" else ("free" if status == "free" else None)
    return {"exam": exam, "status": status, "tier": tier,
            "entitled": status in ("free", "active"), "paid": status == "active"}


def active_subscription(db: Session, account: models.Account, exam: str):
    """The live (active, not-yet-expired) subscription for this exam, if any. Latest expiry wins."""
    return db.scalar(select(models.Subscription).where(
        models.Subscription.account_id == account.id,
        models.Subscription.exam_code == exam,
        models.Subscription.status == "active",
        models.Subscription.expires_at > datetime.utcnow(),
    ).order_by(models.Subscription.expires_at.desc()))


def months_for_plan(plan: "models.PricePlan") -> int:
    """How many months a paid plan grants. Stored in the plan's limits JSON: {'months': 3}."""
    try:
        m = int((plan.limits or {}).get("months", 1))
        return m if m > 0 else 1
    except (TypeError, ValueError):
        return 1


def _add_months(dt: datetime, months: int) -> datetime:
    m = dt.month - 1 + months
    year, month = dt.year + m // 12, m % 12 + 1
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    dim = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return dt.replace(year=year, month=month, day=min(dt.day, dim))


def grant_subscription(db: Session, account: models.Account, plan: "models.PricePlan", *,
                       order_id: str | None = None, rp_payment_id: str | None = None) -> dict:
    """Grant time-bound access for a VERIFIED paid plan: one Subscription per exam + entitlement active.
    A renewal stacks on top of any time remaining. Idempotency is the caller's responsibility
    (guard on PaymentOrder.granted) so a duplicate webhook can't double-grant."""
    if plan.kind == "free":
        raise HTTPException(400, "free tiers are granted, not purchased")
    exams = plan.bundle_exams if plan.kind == "bundle" else [plan.exam_code]
    months = months_for_plan(plan)
    now = datetime.utcnow()
    granted = []
    for ex in exams:
        live = active_subscription(db, account, ex)
        base = live.expires_at if live else now      # stack a renewal onto remaining time
        expires = _add_months(base, months)
        db.add(models.Subscription(account_id=account.id, exam_code=ex, plan_code=plan.code,
                                   order_id=order_id, started_at=now, expires_at=expires, status="active"))
        _upsert_entitlement(db, account, ex, "active")
        granted.append({"exam": ex, "expires_at": expires.isoformat()})
    db.commit()
    from . import warmstart
    for ex in exams:
        warmstart.warm_start(db, account, ex, persist=True)
    return {"granted": granted, "months": months, "plan": plan.code}


def all_entitlements(db: Session, account: models.Account) -> list[dict]:
    rows = db.scalars(select(models.Entitlement).where(
        models.Entitlement.account_id == account.id)).all()
    return [{"exam": e.exam_code, "status": e.status,
             "tier": "paid" if e.status == "active" else e.status} for e in rows]


def _upsert_entitlement(db: Session, account: models.Account, exam: str, status: str) -> None:
    ent = _entitlement(db, account, exam)
    if ent is None:
        db.add(models.Entitlement(account_id=account.id, exam_code=exam, status=status))
    else:
        ent.status = status


def grant_free_tier(db: Session, account: models.Account, exam: str) -> dict:
    """Grant the per-course free tier if the learner has no entitlement yet (idempotent), then
    warm-start the course from any shared signals on the learner's other courses (AC-02)."""
    if _entitlement(db, account, exam) is None:
        _upsert_entitlement(db, account, exam, "free")
        db.commit()
        from . import warmstart
        warmstart.warm_start(db, account, exam, persist=True)
    return entitlement_state(db, account, exam)


def _component_total_cents(db: Session, exams: list[str]) -> int:
    total = 0
    for ex in exams:
        paid = db.scalar(select(models.PricePlan).where(
            models.PricePlan.exam_code == ex, models.PricePlan.kind == "paid",
            models.PricePlan.active.is_(True)))
        if paid:
            total += paid.amount_cents
    return total


def purchase(db: Session, account: models.Account, plan_code: str) -> dict:
    """Record a purchase and grant entitlements. Does NOT charge — integrate a PSP here."""
    plan = db.get(models.PricePlan, plan_code)
    if plan is None or not plan.active:
        raise HTTPException(404, f"unknown plan '{plan_code}'")
    if plan.kind == "free":
        raise HTTPException(400, "free tiers are granted, not purchased — use /billing/grant-free")

    exams = plan.bundle_exams if plan.kind == "bundle" else [plan.exam_code]
    for ex in exams:
        _upsert_entitlement(db, account, ex, "active")

    savings_cents = 0
    if plan.kind == "bundle":
        savings_cents = max(0, _component_total_cents(db, exams) - plan.amount_cents)

    order = models.Order(account_id=account.id, plan_code=plan.code, currency=plan.currency,
                         amount_cents=plan.amount_cents, status="paid",
                         claim_state={"refundable": True, "guarantee": "per_course",
                                      "claimed": False})
    db.add(order)
    db.flush()
    db.commit()
    # warm-start every newly entitled course from the learner's other courses (AC-02)
    from . import warmstart
    for ex in exams:
        warmstart.warm_start(db, account, ex, persist=True)
    return {
        "status": "ok", "order_id": str(order.id), "plan": plan.code, "currency": plan.currency,
        "amount": plan.amount_cents / 100, "granted_exams": exams,
        "bundle_savings": round(savings_cents / 100, 2) if savings_cents else None,
        "note": "order recorded and entitlement(s) granted — no real charge "
                "(integrate Stripe/Razorpay at billing.purchase())",
    }


def free_tier_usage(db: Session, account: models.Account, exam: str,
                    resource: str = "full_mocks") -> dict:
    """How much of a metered free-tier resource the learner has consumed."""
    ent = _entitlement(db, account, exam)
    if ent and ent.status == "active":
        return {"resource": resource, "metered": False, "tier": "paid"}
    plan = db.scalar(select(models.PricePlan).where(
        models.PricePlan.exam_code == exam, models.PricePlan.kind == "free"))
    limit = (plan.limits or {}).get(resource) if plan else None
    used = 0
    if resource == "full_mocks":
        used = db.scalar(select(models.func.count(models.MockSession.id)).where(
            models.MockSession.learner_id == account.id,
            models.MockSession.exam_code == exam,
            models.MockSession.section_key.is_(None))) or 0
    remaining = None if limit is None else max(0, limit - used)
    return {"resource": resource, "metered": True, "tier": "free", "used": used,
            "limit": limit, "remaining": remaining,
            "exhausted": (limit is not None and used >= limit)}


def enforce(db: Session, account: models.Account, exam: str, *, need: str = "any",
            resource: str | None = None) -> dict:
    """Access guard for course-scoped surfaces. No-op unless settings.enforce_entitlements is on.
    need='paid' requires an active paid entitlement; need='any' allows the free tier but blocks once a
    metered free-tier resource is exhausted. Auto-grants the free tier so the free experience works."""
    state = entitlement_state(db, account, exam)
    if not settings.enforce_entitlements:
        return state
    if need == "paid" and not state["paid"]:
        raise HTTPException(402, {"error": "paid_entitlement_required", "exam": exam,
                                  "see": "/billing/catalog"})
    if not state["entitled"]:
        state = grant_free_tier(db, account, exam)   # baseline free access
    if resource is not None and state["tier"] == "free":
        usage = free_tier_usage(db, account, exam, resource)
        if usage.get("exhausted"):
            raise HTTPException(402, {"error": "free_tier_limit_reached", "resource": resource,
                                      "exam": exam, "see": "/billing/catalog"})
    return state


def validate_coupon(db, code: str, exam: str | None = None, amount: int = 0) -> dict:
    """Preview a coupon's discount for an order (amount in paise). Read-only — does not consume a use.
    Returns {valid, discount, final, reason}. This is the backend for 'apply coupon at checkout'."""
    from datetime import datetime

    from sqlalchemy import func, select

    from .. import models
    code = (code or "").strip().upper()
    if not code:
        return {"valid": False, "reason": "Enter a coupon code"}
    c = db.scalar(select(models.Coupon).where(func.upper(models.Coupon.code) == code))
    if c is None:
        return {"valid": False, "reason": "Invalid coupon code"}
    if c.status != "active":
        return {"valid": False, "reason": "This coupon is not active"}

    def _parse(s):
        try:
            return datetime.fromisoformat(s) if s else None
        except (ValueError, TypeError):
            return None

    now = datetime.now()
    vf, vu = _parse(c.valid_from), _parse(c.valid_until)
    if vf and now < vf:
        return {"valid": False, "reason": "This coupon is not yet valid"}
    if vu and now > vu:
        return {"valid": False, "reason": "This coupon has expired"}
    if c.max_total and c.used >= c.max_total:
        return {"valid": False, "reason": "This coupon has reached its usage limit"}
    if c.courses and exam and exam not in c.courses:
        return {"valid": False, "reason": f"This coupon is not valid for {exam}"}
    if c.min_purchase and amount < c.min_purchase:
        return {"valid": False, "reason": "Order value is below the minimum for this coupon"}

    if c.type == "percentage":
        discount = amount * c.value // 100
        if c.max_discount:
            discount = min(discount, c.max_discount)
    else:
        discount = c.value
    discount = max(0, min(discount, amount))
    return {"valid": True, "code": c.code, "type": c.type, "discount": discount,
            "final": amount - discount, "description": c.description or ""}
