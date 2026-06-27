"""Student-management tests — the rich student profile, verification, payment, enrollments,
soft-delete, and real last-login tracking."""
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


def test_student_create_update_delete():
    with TestClient(app) as c:
        A = _admin(c, "stu-admin1@vettalume.test")
        made = c.post("/admin/students", json={
            "name": "Rohan Mehta", "email": "Rohan.Mehta@email.com", "phone": "+91 90000 11111",
            "status": "active", "regType": "paid", "purchasedCourse": "CAT", "progress": 62,
            "verified": True, "payment": {"status": "successful", "amount": 490000, "method": "UPI"},
            "exams": ["CAT"],
        }, headers=A)
        assert made.status_code == 200
        s = made.json()
        sid = s["id"]
        assert s["name"] == "Rohan Mehta" and s["email"] == "rohan.mehta@email.com"
        assert s["phone"] == "+91 90000 11111" and s["regType"] == "paid"
        assert s["verified"] is True and s["progress"] == 62 and s["exams"] == ["CAT"]
        assert s["payment"]["status"] == "successful" and s["payment"]["amount"] == 490000

        # appears in the roster
        lst = c.get("/admin/students", headers=A).json()
        assert any(x["id"] == sid for x in lst["students"])

        # update: rename, change progress, add GMAT, drop CAT
        upd = c.put(f"/admin/students/{sid}", json={
            "name": "Rohan M.", "email": "rohan.mehta@email.com", "phone": "+91 90000 11111",
            "status": "inactive", "regType": "paid", "purchasedCourse": "GMAT", "progress": 80,
            "verified": True, "payment": s["payment"], "exams": ["GMAT"],
        }, headers=A).json()
        assert upd["name"] == "Rohan M." and upd["status"] == "inactive" and upd["progress"] == 80
        assert upd["exams"] == ["GMAT"]  # CAT removed, GMAT added

        # soft delete -> gone from the roster, but the endpoint still resolves it
        assert c.delete(f"/admin/students/{sid}", headers=A).json()["ok"] is True
        lst2 = c.get("/admin/students", headers=A).json()
        assert not any(x["id"] == sid for x in lst2["students"])


def test_student_verify_payment_enrollments():
    with TestClient(app) as c:
        A = _admin(c, "stu-admin2@vettalume.test")
        sid = c.post("/admin/students", json={"name": "Priya", "email": "priya@email.com"}, headers=A).json()["id"]

        # verify toggles
        assert c.post(f"/admin/students/{sid}/verify", headers=A).json()["verified"] is True
        assert c.post(f"/admin/students/{sid}/verify", headers=A).json()["verified"] is False

        # payment with autoVerify -> marks verified when successful
        r = c.post(f"/admin/students/{sid}/payment",
                   json={"status": "successful", "amount": 690000, "method": "Card", "autoVerify": True},
                   headers=A).json()
        assert r["payment"]["status"] == "successful" and r["payment"]["amount"] == 690000
        assert r["verified"] is True  # auto-verified

        # enrollments sync to an exact set
        r = c.put(f"/admin/students/{sid}/enrollments", json={"exams": ["CAT", "GRE"]}, headers=A).json()
        assert set(r["exams"]) == {"CAT", "GRE"}
        r = c.put(f"/admin/students/{sid}/enrollments", json={"exams": ["GRE"]}, headers=A).json()
        assert r["exams"] == ["GRE"]


def test_student_requires_admin():
    with TestClient(app) as c:
        _admin(c, "stu-gate@vettalume.test")
        L = {"Authorization": "Bearer " + c.post("/auth/dev-login", json={"email": "plain@x.com"}).json()["access_token"]}
        assert c.get("/admin/students", headers=L).status_code == 403
        assert c.post("/admin/students", json={"name": "X", "email": "x@x.com"}, headers=L).status_code == 403


def test_last_login_is_tracked():
    with TestClient(app) as c:
        A = _admin(c, "stu-admin3@vettalume.test")
        # a learner signs in via dev-login -> last_login should be recorded
        c.post("/auth/dev-login", json={"email": "tracked@x.com"})
        lst = c.get("/admin/students", headers=A).json()["students"]
        row = next(x for x in lst if x["email"] == "tracked@x.com")
        assert row["lastLogin"] != "—" and len(row["lastLogin"]) >= 10  # a real timestamp string
