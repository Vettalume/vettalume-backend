"""Coupon / discount-code tests — admin CRUD, toggle, and the checkout validation logic."""
import os

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["SERVE_ONLY_APPROVED"] = "true"

from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402


def _admin(c, email):
    cur = {e.strip() for e in (settings.admin_emails or "").split(",") if e.strip()}
    cur.add(email)
    settings.admin_emails = ",".join(cur)
    r = c.post("/auth/register", json={"email": email, "password": "adminpass123"}).json()
    return {"Authorization": "Bearer " + r["access_token"]}


def test_coupon_crud_and_toggle():
    with TestClient(app) as c:
        A = _admin(c, "coupon-admin@vettalume.test")
        # create
        made = c.post("/admin/coupons", json={
            "code": "save20", "type": "percentage", "value": 20, "maxTotal": 100,
            "maxPerUser": 1, "minPurchase": 500000, "maxDiscount": 200000,
            "validFrom": "2026-01-01T00:00", "validUntil": "2026-12-31T23:59",
            "description": "Launch offer", "attempt": "all", "courses": ["CAT"],
        }, headers=A)
        assert made.status_code == 200
        cp = made.json()
        assert cp["code"] == "SAVE20" and cp["maxDiscount"] == 200000 and cp["used"] == 0 and cp["status"] == "active"
        cid = cp["id"]
        # list
        lst = c.get("/admin/coupons", headers=A).json()
        assert lst["count"] == 1 and lst["coupons"][0]["code"] == "SAVE20"
        # update
        upd = c.put(f"/admin/coupons/{cid}", json={**cp, "value": 25, "code": "SAVE25"}, headers=A).json()
        assert upd["value"] == 25 and upd["code"] == "SAVE25"
        # toggle
        assert c.post(f"/admin/coupons/{cid}/toggle", headers=A).json()["status"] == "inactive"
        assert c.post(f"/admin/coupons/{cid}/toggle", headers=A).json()["status"] == "active"
        # delete
        assert c.delete(f"/admin/coupons/{cid}", headers=A).json()["deleted"] == "SAVE25"
        assert c.get("/admin/coupons", headers=A).json()["count"] == 0


def test_coupon_duplicate_code_rejected():
    with TestClient(app) as c:
        A = _admin(c, "coupon-dup@vettalume.test")
        c.post("/admin/coupons", json={"code": "DUP", "type": "fixed", "value": 10000}, headers=A)
        again = c.post("/admin/coupons", json={"code": "dup", "type": "fixed", "value": 5000}, headers=A)
        assert again.status_code == 409


def test_coupon_requires_admin():
    with TestClient(app) as c:
        _admin(c, "coupon-gate@vettalume.test")  # some admin exists, but we call as a normal learner
        L = {"Authorization": "Bearer " + c.post("/auth/dev-login", json={"email": "nonadmin@x.com"}).json()["access_token"]}
        assert c.get("/admin/coupons", headers=L).status_code == 403
        assert c.post("/admin/coupons", json={"code": "X", "type": "fixed", "value": 1}, headers=L).status_code == 403


def test_coupon_validation_logic():
    with TestClient(app) as c:
        A = _admin(c, "coupon-val@vettalume.test")
        # 20% off, min ₹5000 (500000 paise), cap ₹2000 (200000), CAT only, valid window
        c.post("/admin/coupons", json={
            "code": "PCT20", "type": "percentage", "value": 20, "maxTotal": 0, "maxPerUser": 0,
            "minPurchase": 500000, "maxDiscount": 200000, "validFrom": "2026-01-01T00:00",
            "validUntil": "2099-12-31T23:59", "courses": ["CAT"],
        }, headers=A)
        # ₹6000 order -> 20% = ₹1200 (under cap)
        r = c.post("/billing/coupon/validate", json={"code": "PCT20", "exam": "CAT", "amount": 600000}).json()
        assert r["valid"] and r["discount"] == 120000 and r["final"] == 480000
        # ₹20000 order -> 20% = ₹4000 but capped at ₹2000
        r = c.post("/billing/coupon/validate", json={"code": "PCT20", "exam": "CAT", "amount": 2000000}).json()
        assert r["valid"] and r["discount"] == 200000
        # below minimum purchase -> invalid
        r = c.post("/billing/coupon/validate", json={"code": "PCT20", "exam": "CAT", "amount": 100000}).json()
        assert not r["valid"]
        # wrong course -> invalid
        r = c.post("/billing/coupon/validate", json={"code": "PCT20", "exam": "GMAT", "amount": 600000}).json()
        assert not r["valid"]
        # unknown code -> invalid
        assert not c.post("/billing/coupon/validate", json={"code": "NOPE", "amount": 600000}).json()["valid"]

        # fixed ₹500 off, and inactive-toggle blocks it
        fixed = c.post("/admin/coupons", json={"code": "FLAT500", "type": "fixed", "value": 50000}, headers=A).json()
        r = c.post("/billing/coupon/validate", json={"code": "FLAT500", "amount": 600000}).json()
        assert r["valid"] and r["discount"] == 50000 and r["final"] == 550000
        c.post(f"/admin/coupons/{fixed['id']}/toggle", headers=A)  # deactivate
        assert not c.post("/billing/coupon/validate", json={"code": "FLAT500", "amount": 600000}).json()["valid"]

        # expired coupon -> invalid
        c.post("/admin/coupons", json={"code": "OLD", "type": "fixed", "value": 10000,
                                       "validUntil": "2020-01-01T00:00"}, headers=A)
        assert not c.post("/billing/coupon/validate", json={"code": "OLD", "amount": 600000}).json()["valid"]
