from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..deps import get_current_learner, get_db
from ..services import billing

router = APIRouter(prefix="/billing", tags=["billing"])


class PurchaseIn(BaseModel):
    plan_code: str


@router.get("/catalog")
def get_catalog(db: Session = Depends(get_db)) -> dict:
    """The SKU catalog: per-course free + paid tiers (multi-currency) and the multi-exam bundle."""
    return {"plans": billing.catalog(db)}


@router.get("/entitlements")
def get_entitlements(learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    return {"entitlements": billing.all_entitlements(db, learner)}


@router.get("/access")
def access(exam: str, learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    if db.get(models.Exam, exam) is None:
        raise HTTPException(404, f"unknown exam '{exam}'")
    state = billing.entitlement_state(db, learner, exam)
    state["free_tier_usage"] = billing.free_tier_usage(db, learner, exam, "full_mocks")
    state["enforcement_on"] = billing.settings.enforce_entitlements
    return state


@router.post("/grant-free")
def grant_free(exam: str, learner=Depends(get_current_learner),
               db: Session = Depends(get_db)) -> dict:
    if db.get(models.Exam, exam) is None:
        raise HTTPException(404, f"unknown exam '{exam}'")
    return billing.grant_free_tier(db, learner, exam)


@router.post("/purchase")
def purchase(body: PurchaseIn, learner=Depends(get_current_learner),
             db: Session = Depends(get_db)) -> dict:
    """Record a purchase and grant entitlements. Does not move money (no PSP wired)."""
    return billing.purchase(db, learner, body.plan_code)


class CouponCheckIn(BaseModel):
    code: str
    exam: str | None = None
    amount: int = 0


@router.post("/coupon/validate")
def validate_coupon_endpoint(body: CouponCheckIn, db: Session = Depends(get_db)) -> dict:
    """Preview the discount a coupon gives for an order. Public (no auth) — it only reads."""
    return billing.validate_coupon(db, body.code, body.exam, body.amount)
