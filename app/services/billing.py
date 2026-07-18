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
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from ..config import settings

# ---------------------------------------------------------------------------------------------------
# Tier catalog. Access = a plan (WHAT you get, in `limits`) + a subscription (HOW LONG, expires_at),
# per exam. `limits` knobs:
#   content            : "all" | "sample"
#   sample_chapters    : first N chapters/section unlocked (when content == "sample")
#   sample_subtopics   : first M subtopics within those chapters (null => whole chapter)
#   sectional_per_section / full_mocks : mock quotas (int; omit => unlimited) — total pool per subscription
#   practice           : adaptive practice included (bool)
#   diagnostic         : diagnostic test included (bool)
#   days / months      : validity (trial uses days; paid uses months)
# ensure_catalog() is AUTHORITATIVE: it upserts these limits every boot, so this code is the single
# source of truth for who-gets-what. Prices are placeholders — tune freely.
# ---------------------------------------------------------------------------------------------------

# Free (registered, no purchase): the 1st chapter of each section, no mocks, no practice.
FREE_LIMITS = {"content": "sample", "sample_chapters": 1,
               "sectional_per_section": 0, "full_mocks": 0, "practice": False, "diagnostic": True}
# 7-day trial (one-time per exam): 1st chapter of each section + a couple of mocks.
TRIAL_LIMITS = {"days": 7, "content": "sample", "sample_chapters": 1,
                "sectional_per_section": 2, "full_mocks": 2, "practice": False, "diagnostic": True}

_FREE_PLANS = [("CAT", "INR"), ("GMAT", "USD"), ("GRE", "USD")]
_TRIAL_PLANS = [("CAT", "INR"), ("GMAT", "USD"), ("GRE", "USD")]

# Legacy one-time paid SKUs + the multi-exam bundle. Not part of the new month-pass model, but kept
# (create-only) so existing integrations/tests keep working. Frontend sells the month passes instead.
_LEGACY_CATALOG = [
    dict(code="gmat_summit", kind="paid", exam_code="GMAT", name="GMAT Summit",
         currency="USD", amount_cents=19900, period="one_time", limits=None),
    dict(code="gre_core", kind="paid", exam_code="GRE", name="GRE Core",
         currency="USD", amount_cents=14900, period="one_time", limits=None),
    dict(code="cat_pro", kind="paid", exam_code="CAT", name="CAT Pro",
         currency="INR", amount_cents=1499900, period="one_time", limits=None),
    dict(code="bundle_gmat_gre", kind="bundle", exam_code=None, name="GMAT + GRE Bundle",
         currency="USD", amount_cents=29900, period="one_time", bundle_exams=["GMAT", "GRE"], limits=None),
]

# Paid month-passes: (code, exam, name, paise, months). Limits are COMPUTED from months by
# _paid_limits() so every plan stays consistent.
_SUBSCRIPTION_PLANS = [
    ("cat_1m",  "CAT",  "CAT — 1 Month",     99900,  1),
    ("cat_3m",  "CAT",  "CAT — 3 Months",   249900,  3),
    ("cat_6m",  "CAT",  "CAT — 6 Months",   399900,  6),
    ("cat_12m", "CAT",  "CAT — 12 Months",  599900, 12),
    ("gmat_1m", "GMAT", "GMAT — 1 Month",   129900,  1),
    ("gmat_3m", "GMAT", "GMAT — 3 Months",  299900,  3),
    ("gmat_6m", "GMAT", "GMAT — 6 Months",  499900,  6),
    ("gmat_12m","GMAT", "GMAT — 12 Months", 999900, 12),
    ("gre_1m",  "GRE",  "GRE — 1 Month",    119900,  1),
    ("gre_3m",  "GRE",  "GRE — 3 Months",   279900,  3),
    ("gre_6m",  "GRE",  "GRE — 6 Months",   459900,  6),
    ("gre_12m", "GRE",  "GRE — 12 Months",  899900, 12),
]


def _paid_limits(months: int) -> dict:
    """A paid plan's `limits`, derived from its length:
    12m -> everything + unlimited mocks; 1m -> limited learning (first 2 chapters/section) + 20/20;
    3m/6m -> full learning + 20 sectional/section + 20 full PER MONTH (total pool = 20 x months)."""
    base = {"months": months, "practice": True, "diagnostic": True}
    if months >= 12:
        return {**base, "content": "all"}                              # unlimited mocks (no caps)
    if months == 1:
        return {**base, "content": "sample", "sample_chapters": 1,   # 1st chapter of each section
                "sectional_per_section": 20, "full_mocks": 20}
    return {**base, "content": "all",
            "sectional_per_section": 20 * months, "full_mocks": 20 * months}


def _upsert_plan(db: Session, *, code, kind, exam_code, name, currency, amount_cents, period,
                 limits) -> bool:
    """Create a managed plan, or keep its limits/active state authoritative if it already exists
    (price/name are left as-is on an existing row)."""
    p = db.get(models.PricePlan, code)
    if p is None:
        db.add(models.PricePlan(code=code, kind=kind, exam_code=exam_code, name=name, currency=currency,
                                amount_cents=amount_cents, period=period, active=True, limits=limits))
        return True
    changed = False
    if p.limits != limits:
        p.limits = limits; changed = True
    if not p.active:
        p.active = True; changed = True
    return changed


def ensure_catalog(db: Session) -> None:
    """Load + keep the managed tier catalog authoritative (limits upserted every boot)."""
    changed = False
    for spec in _LEGACY_CATALOG:                 # create-only (kept for compat)
        if db.get(models.PricePlan, spec["code"]) is None:
            db.add(models.PricePlan(active=True, **spec)); changed = True
    for exam, cur in _FREE_PLANS:
        changed |= _upsert_plan(db, code=f"{exam.lower()}_free", kind="free", exam_code=exam,
                                name=f"{exam} Free", currency=cur, amount_cents=0, period="one_time",
                                limits=dict(FREE_LIMITS))
    for exam, cur in _TRIAL_PLANS:
        changed |= _upsert_plan(db, code=f"{exam.lower()}_trial", kind="trial", exam_code=exam,
                                name=f"{exam} 7-Day Trial", currency=cur, amount_cents=0,
                                period="one_time", limits=dict(TRIAL_LIMITS))
    for code, exam, name, paise, months in _SUBSCRIPTION_PLANS:
        changed |= _upsert_plan(db, code=code, kind="paid", exam_code=exam, name=name, currency="INR",
                                amount_cents=paise, period="subscription", limits=_paid_limits(months))
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
    if status in ("active", "trial"):
        # A subscription-backed entitlement (paid OR trial) is valid only while a subscription is live.
        # An admin manual grant (no Subscription row at all) is permanent and is left untouched.
        has_sub = db.scalar(select(models.Subscription.id).where(
            models.Subscription.account_id == account.id,
            models.Subscription.exam_code == exam).limit(1))
        if has_sub is not None and active_subscription(db, account, exam) is None:
            status = "expired"       # trial ran out / subscription lapsed
    tier = ("paid" if status == "active" else "trial" if status == "trial"
            else "free" if status == "free" else None)
    return {"exam": exam, "status": status, "tier": tier,
            "entitled": status in ("free", "active", "trial"),
            "paid": status == "active", "trial": status == "trial"}


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


# ----------------------------- free trial (7-day, one per account per exam) -----------------------------

def _trial_code(exam: str) -> str:
    return f"{exam.lower()}_trial"


def _trial_plan(db: Session, exam: str):
    return db.get(models.PricePlan, _trial_code(exam))


def _trial_days(db: Session, exam: str) -> int:
    p = _trial_plan(db, exam)
    try:
        return int((p.limits or {}).get("days", 7)) if p else 7
    except (TypeError, ValueError):
        return 7


def has_used_trial(db: Session, account: models.Account, exam: str) -> bool:
    """True if this account ever started a trial for this exam (live OR expired) — trials are one-shot."""
    return db.scalar(select(models.Subscription.id).where(
        models.Subscription.account_id == account.id,
        models.Subscription.exam_code == exam,
        models.Subscription.plan_code == _trial_code(exam)).limit(1)) is not None


def _count_full_attempts(db: Session, account: models.Account, exam: str, since=None) -> int:
    q = select(func.count(models.MockAttempt.id)).where(
        models.MockAttempt.learner_id == account.id,
        models.MockAttempt.exam_code == exam,
        models.MockAttempt.mock_type == "full")
    if since is not None:
        q = q.where(models.MockAttempt.created_at >= since)
    return db.scalar(q) or 0


def _count_sectional_attempts(db: Session, account: models.Account, exam: str, section_key, since=None) -> int:
    q = select(func.count(models.MockAttempt.id)).where(
        models.MockAttempt.learner_id == account.id,
        models.MockAttempt.exam_code == exam,
        models.MockAttempt.mock_type == "sectional",
        models.MockAttempt.section_key == section_key)
    if since is not None:
        q = q.where(models.MockAttempt.created_at >= since)
    return db.scalar(q) or 0


def _tier_plan(db: Session, account: models.Account, exam: str, state: dict):
    """The PricePlan governing this learner's current access to `exam`: the paid subscription's plan,
    the trial plan, or the free plan (or None)."""
    tier = state["tier"]
    if tier == "paid":
        sub = active_subscription(db, account, exam)
        return db.get(models.PricePlan, sub.plan_code) if sub else None
    if tier == "trial":
        return _trial_plan(db, exam)
    if tier == "free":
        return db.scalar(select(models.PricePlan).where(
            models.PricePlan.exam_code == exam, models.PricePlan.kind == "free"))
    return None


def _tier_since(db: Session, account: models.Account, exam: str, state: dict):
    """Mock quotas are scoped to the current subscription (trial/paid); free tier counts all-time."""
    if state["tier"] in ("paid", "trial"):
        sub = active_subscription(db, account, exam)
        return sub.started_at if sub else None
    return None


def start_trial(db: Session, account: models.Account, exam: str) -> dict:
    """Grant a one-time 7-day trial for ONE exam (a short Subscription + a 'trial' entitlement).
    Rejects if the exam is already paid, or the trial was already used for this exam. Idempotent while
    a trial is live (returns the current status)."""
    exam = (exam or "").upper()
    plan = _trial_plan(db, exam)
    if plan is None:
        raise HTTPException(404, f"no trial plan for '{exam}'")
    st = entitlement_state(db, account, exam)
    if st["paid"]:
        raise HTTPException(400, {"error": "already_entitled",
                                  "detail": "You already have paid access to this exam."})
    if st["trial"]:
        return trial_status(db, account, exam)             # already on trial -> idempotent
    if has_used_trial(db, account, exam):
        raise HTTPException(409, {"error": "trial_already_used",
                                  "detail": "You've already used your free trial for this exam."})
    now = datetime.utcnow()
    expires = now + timedelta(days=_trial_days(db, exam))
    db.add(models.Subscription(account_id=account.id, exam_code=exam, plan_code=plan.code,
                               started_at=now, expires_at=expires, status="active"))
    _upsert_entitlement(db, account, exam, "trial")
    prof = db.get(models.StudentProfile, account.id)
    if prof is not None and prof.reg_type == "registered":
        prof.reg_type = "trial"
    db.commit()
    from . import warmstart
    warmstart.warm_start(db, account, exam, persist=True)
    return trial_status(db, account, exam)


def trial_status(db: Session, account: models.Account, exam: str) -> dict:
    """Access state for the dashboard: current tier, days left, and mock usage vs the tier's quotas."""
    exam = (exam or "").upper()
    st = entitlement_state(db, account, exam)
    plan = _tier_plan(db, account, exam, st) or _trial_plan(db, exam)
    limits = (plan.limits if plan else {}) or {}
    on_trial = st["tier"] == "trial"
    sub = active_subscription(db, account, exam) if st["tier"] in ("trial", "paid") else None
    since = sub.started_at if sub else None
    days_left, expires_at = None, None
    if sub is not None:
        expires_at = sub.expires_at.isoformat()
        secs = int((sub.expires_at - datetime.utcnow()).total_seconds())
        days_left = max(0, -(-secs // 86400))              # ceil to whole days
    sections = db.scalars(select(models.Section).where(models.Section.exam_code == exam)).all()
    sectional_used = {s.key: _count_sectional_attempts(db, account, exam, s.key, since) for s in sections}
    return {
        "exam": exam, "tier": st["tier"], "status": st["status"],
        "on_trial": on_trial, "paid": st["paid"],
        "can_start_trial": (not st["paid"]) and (not st["trial"]) and not has_used_trial(db, account, exam),
        "days_left": days_left, "expires_at": expires_at,
        "limits": {"sectional_per_section": limits.get("sectional_per_section"),
                   "full_mocks": limits.get("full_mocks"), "content": limits.get("content"),
                   "practice": limits.get("practice")},
        "used": {"full_mocks": _count_full_attempts(db, account, exam, since), "sectional": sectional_used},
    }


def _tier_or_lock(db: Session, account: models.Account, exam: str) -> dict:
    """Resolve the tier for a gated surface. Expired (trial/paid lapsed) -> 402 upgrade. A brand-new
    learner (no entitlement) is granted the free tier so the free experience keeps working."""
    st = entitlement_state(db, account, exam)
    if st["status"] == "expired":
        raise HTTPException(402, {"error": "upgrade_required", "exam": exam, "see": "/pricing",
                                  "detail": "Your access to this exam has ended. Upgrade to keep going."})
    if st["status"] is None:
        st = grant_free_tier(db, account, exam)
    return st


def guard_mock_start(db: Session, account: models.Account, mock) -> None:
    """Raise 402 if starting this mock exceeds the learner's plan quota. No-op unless enforcement is on.
    The quota comes from the active plan's limits (sectional_per_section / full_mocks); an omitted limit
    means unlimited (e.g. 12-month). Counts are scoped to the current subscription. Expired -> locked."""
    if not settings.enforce_entitlements:
        return
    exam = (mock.exam_code or "").upper()
    st = _tier_or_lock(db, account, exam)
    plan = _tier_plan(db, account, exam, st)
    limits = (plan.limits or {}) if plan else {}
    is_full = (mock.type or "").lower() == "full"
    limit = limits.get("full_mocks" if is_full else "sectional_per_section")
    if limit is None:
        return                                   # unlimited (or unset) for this plan
    since = _tier_since(db, account, exam, st)
    if is_full:
        used, section = _count_full_attempts(db, account, exam, since), None
    else:
        from .student_mocks import _primary_section_key
        section = _primary_section_key(mock)
        used = _count_sectional_attempts(db, account, exam, section, since)
    if used >= limit:
        err = ("trial_limit_reached" if st["trial"]
               else "free_tier_limit_reached" if st["tier"] == "free" else "plan_limit_reached")
        detail = {"error": err, "resource": ("full_mocks" if is_full else "sectional_mocks"),
                  "exam": exam, "limit": limit, "used": used, "see": "/pricing"}
        if section:
            detail["section"] = section
        raise HTTPException(402, detail)


def _sample_concept_ids(db: Session, exam: str, sample_chapters: int = 1,
                        sample_subtopics: int | None = None) -> set[str]:
    """The 'sample' content set: the first `sample_chapters` chapters (topic nodes) of each section, and
    within each the first `sample_subtopics` subtopics (concepts) — or all of them if that is None.
    Chapter/subtopic order is creation order (deterministic default; admins can reorder content)."""
    ids: set[str] = set()
    for s in db.scalars(select(models.Section).where(models.Section.exam_code == exam)).all():
        topics = db.scalars(select(models.KnowledgeNode).where(
            models.KnowledgeNode.section_id == s.id,
            models.KnowledgeNode.kind == models.NodeKind.topic.value,
        ).order_by(models.KnowledgeNode.created_at.asc()).limit(max(0, sample_chapters))).all()
        for t in topics:
            q = select(models.KnowledgeNode.id).where(
                models.KnowledgeNode.parent_id == t.id,
                models.KnowledgeNode.kind == models.NodeKind.concept.value,
            ).order_by(models.KnowledgeNode.created_at.asc())
            if sample_subtopics is not None:
                q = q.limit(sample_subtopics)
            ids.update(db.scalars(q).all())
    return ids


def guard_content(db: Session, account: models.Account, node) -> None:
    """Raise 402 if the learner's plan gives only sample content and this concept is outside the sample.
    No-op unless enforcement is on; plans with content=="all" pass; topic/chapter nodes always pass."""
    if not settings.enforce_entitlements:
        return
    if getattr(node, "kind", None) != models.NodeKind.concept.value:
        return
    exam = (node.exam_code or "").upper()
    st = _tier_or_lock(db, account, exam)
    plan = _tier_plan(db, account, exam, st)
    limits = (plan.limits or {}) if plan else {}
    if limits.get("content") != "sample":
        return   # "all", unset, legacy paid, or a manual admin grant (no subscription) -> full content
    allowed = _sample_concept_ids(db, exam, limits.get("sample_chapters", 1), limits.get("sample_subtopics"))
    if node.id not in allowed:
        raise HTTPException(402, {"error": "content_locked", "exam": exam, "see": "/pricing",
                                  "detail": "This is available on a higher plan. Upgrade to unlock all content."})


def content_access(db: Session, account: models.Account, exam: str) -> dict:
    """What content the learner can open, for the overview's lock icons: {'all': True} for full access,
    else {'all': False, 'concepts': <set of unlocked concept ids>}. Respects enforcement (all when off)."""
    exam = (exam or "").upper()
    if not settings.enforce_entitlements:
        return {"all": True}
    st = entitlement_state(db, account, exam)
    if st["status"] == "expired":
        return {"all": False, "concepts": set()}           # trial/plan lapsed -> everything locked
    if st["status"] is None:
        st = grant_free_tier(db, account, exam)
    plan = _tier_plan(db, account, exam, st)
    limits = (plan.limits or {}) if plan else {}
    if limits.get("content") != "sample":
        return {"all": True}                                # paid "all", legacy, or manual admin grant
    return {"all": False,
            "concepts": _sample_concept_ids(db, exam, limits.get("sample_chapters", 1),
                                            limits.get("sample_subtopics"))}


def guard_practice(db: Session, account: models.Account, exam: str) -> None:
    """Raise 402 if the learner's plan doesn't include adaptive practice. No-op unless enforcement is on."""
    if not settings.enforce_entitlements:
        return
    exam = (exam or "").upper()
    st = _tier_or_lock(db, account, exam)
    plan = _tier_plan(db, account, exam, st)
    if plan is not None and not (plan.limits or {}).get("practice", True):
        raise HTTPException(402, {"error": "practice_locked", "exam": exam, "see": "/pricing",
                                  "detail": "Adaptive practice is available on a paid plan. Upgrade to unlock it."})


def guard_diagnostic(db: Session, account: models.Account, exam: str) -> None:
    """Raise 402 if the learner's plan doesn't include the diagnostic. No-op unless enforcement is on."""
    if not settings.enforce_entitlements:
        return
    exam = (exam or "").upper()
    st = _tier_or_lock(db, account, exam)
    plan = _tier_plan(db, account, exam, st)
    if plan is not None and not (plan.limits or {}).get("diagnostic", True):
        raise HTTPException(402, {"error": "diagnostic_locked", "exam": exam, "see": "/pricing",
                                  "detail": "The diagnostic test isn't included on your current plan."})


def validate_coupon(db, code: str, exam: str | None = None, amount: int = 0) -> dict:
    """Preview a coupon's discount for an order (amount in paise). Read-only — does not consume a use.
    Returns {valid, discount, final, reason}. This is the backend for 'apply coupon at checkout'."""
    from datetime import datetime, timedelta

    from sqlalchemy import func, select

    from .. import models
    def _norm(s):
        return "".join((s or "").split()).upper()   # ignore all whitespace + case
    code = _norm(code)
    if not code:
        return {"valid": False, "reason": "Enter a coupon code"}
    # Small table — match on the whitespace/case-insensitive code so "TEST 90" == "test90".
    c = next((x for x in db.scalars(select(models.Coupon)).all() if _norm(x.code) == code), None)
    if c is None:
        return {"valid": False, "reason": "Invalid coupon code"}
    if c.status != "active":
        return {"valid": False, "reason": "This coupon is not active"}

    def _parse(s):
        try:
            return datetime.fromisoformat(s) if s else None
        except (ValueError, TypeError):
            return None

    # Admin valid-from/until are naive datetime-local wall-clock in IST (India product), so compare
    # against "now" in IST — not the server's UTC — or a just-created coupon reads as "not yet valid".
    now = datetime.utcnow() + timedelta(hours=5, minutes=30)
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
