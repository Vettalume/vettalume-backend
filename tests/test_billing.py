"""Phase 5 — billing/entitlements: catalog, purchases, bundles, free tiers, and enforcement."""
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import pytest
from fastapi import HTTPException

from app import models
from app.services import billing


def _session():
    eng = create_engine("sqlite+pysqlite:///:memory:",
                        connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    db = Session(eng, autoflush=False)
    for code in ("GMAT", "GRE", "CAT"):
        db.add(models.Exam(code=code, name=code))
    db.commit()
    billing.ensure_catalog(db)
    return db


def _acct(db):
    a = models.Account(email="b@t.com", display_name="b"); db.add(a); db.flush(); db.commit()
    return a


def test_catalog_is_idempotent_and_multicurrency():
    db = _session()
    billing.ensure_catalog(db); billing.ensure_catalog(db)   # repeat must not duplicate
    plans = billing.catalog(db)
    assert len(plans) == len(db.scalars(select(models.PricePlan)).all())
    currencies = {p["currency"] for p in plans}
    assert {"USD", "INR"} <= currencies


def test_grant_free_tier_is_idempotent():
    db = _session(); a = _acct(db)
    billing.grant_free_tier(db, a, "CAT")
    billing.grant_free_tier(db, a, "CAT")
    ents = db.scalars(select(models.Entitlement).where(
        models.Entitlement.account_id == a.id)).all()
    assert len(ents) == 1 and ents[0].status == "free"


def test_purchase_records_order_and_grants_active():
    db = _session(); a = _acct(db)
    out = billing.purchase(db, a, "gmat_summit")
    assert out["granted_exams"] == ["GMAT"]
    assert billing.entitlement_state(db, a, "GMAT")["paid"] is True
    orders = db.scalars(select(models.Order).where(models.Order.account_id == a.id)).all()
    assert len(orders) == 1 and orders[0].status == "paid"


def test_bundle_grants_both_and_reports_savings():
    db = _session(); a = _acct(db)
    out = billing.purchase(db, a, "bundle_gmat_gre")
    assert set(out["granted_exams"]) == {"GMAT", "GRE"}
    # 199 + 149 components vs 299 bundle -> 49 saved
    assert out["bundle_savings"] == 49.0
    assert billing.entitlement_state(db, a, "GMAT")["paid"]
    assert billing.entitlement_state(db, a, "GRE")["paid"]


def test_cannot_purchase_a_free_plan():
    db = _session(); a = _acct(db)
    with pytest.raises(HTTPException) as ei:
        billing.purchase(db, a, "gmat_free")
    assert ei.value.status_code == 400


def test_free_tier_usage_counts_full_mocks():
    db = _session(); a = _acct(db)
    billing.grant_free_tier(db, a, "CAT")
    db.add(models.MockSession(learner_id=a.id, exam_code="CAT", section_key=None, mode="fixed_form",
                              status="completed", stage="done", cursor=0, theta=0.0, se=1.0,
                              reliability=0.0, n_answered=0, max_items=10, se_target=0.3,
                              plan={}, seed=1))
    db.commit()
    usage = billing.free_tier_usage(db, a, "CAT")
    assert usage["used"] == 1 and usage["limit"] == 0 and usage["exhausted"] is True


def test_enforcement_is_off_by_default_and_gates_when_on(monkeypatch):
    db = _session(); a = _acct(db)
    # off: even a paid-only surface passes (open demo)
    monkeypatch.setattr(billing.settings, "enforce_entitlements", False)
    assert billing.enforce(db, a, "GMAT", need="paid")["paid"] is False
    # on + only free: paid surface blocked, free surface allowed (auto-grants free)
    monkeypatch.setattr(billing.settings, "enforce_entitlements", True)
    with pytest.raises(HTTPException) as ei:
        billing.enforce(db, a, "GMAT", need="paid")
    assert ei.value.status_code == 402
    state = billing.enforce(db, a, "GMAT", need="any")
    assert state["entitled"] is True and state["tier"] == "free"


def test_enforcement_blocks_when_free_resource_exhausted(monkeypatch):
    db = _session(); a = _acct(db)
    billing.grant_free_tier(db, a, "CAT")
    db.add(models.MockSession(learner_id=a.id, exam_code="CAT", section_key=None, mode="fixed_form",
                              status="completed", stage="done", cursor=0, theta=0.0, se=1.0,
                              reliability=0.0, n_answered=0, max_items=10, se_target=0.3,
                              plan={}, seed=1))
    db.commit()
    monkeypatch.setattr(billing.settings, "enforce_entitlements", True)
    with pytest.raises(HTTPException) as ei:
        billing.enforce(db, a, "CAT", need="any", resource="full_mocks")
    assert ei.value.status_code == 402
