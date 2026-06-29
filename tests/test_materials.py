"""Study-materials tests — upload, list, gated download, delete, admin gating, entitlement gate."""
import os
import uuid as _u

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


def _make_concept(c, A):
    tid = "tmat-" + _u.uuid4().hex[:6]
    cid = "cmat-" + _u.uuid4().hex[:6]
    c.post("/admin/topics", json={"id": tid, "exam_code": "CAT", "section_key": "QA", "name": "Mat Topic"}, headers=A)
    c.post("/admin/concepts", json={"id": cid, "exam_code": "CAT", "section_key": "QA",
                                    "name": "Mat Concept", "parent_id": tid}, headers=A)
    return cid


def test_material_upload_list_download_delete():
    with TestClient(app) as c:
        A = _admin(c, "mat-admin@vettalume.test")
        cid = _make_concept(c, A)
        pdf = b"%PDF-1.4 " + b"x" * 2000
        up = c.post(f"/admin/concepts/{cid}/materials",
                    files={"file": ("notes.pdf", pdf, "application/pdf")},
                    data={"title": "Averages formula sheet"}, headers=A)
        assert up.status_code == 200
        m = up.json()
        mid = m["id"]
        assert m["title"] == "Averages formula sheet" and m["filename"] == "notes.pdf"
        assert m["size"] == len(pdf) and m["sizeLabel"].endswith("KB")
        # list
        lst = c.get(f"/admin/concepts/{cid}/materials", headers=A).json()
        assert lst["count"] == 1 and lst["materials"][0]["id"] == mid
        # download returns the exact bytes + content-type
        dl = c.get(f"/learn/materials/{mid}/download", headers=A)
        assert dl.status_code == 200 and dl.content == pdf
        assert dl.headers["content-type"].startswith("application/pdf")
        # delete
        assert c.delete(f"/admin/materials/{mid}", headers=A).json()["ok"] is True
        assert c.get(f"/admin/concepts/{cid}/materials", headers=A).json()["count"] == 0


def test_material_upload_requires_admin():
    with TestClient(app) as c:
        A = _admin(c, "mat-gate@vettalume.test")
        cid = _make_concept(c, A)
        L = {"Authorization": "Bearer " + c.post("/auth/dev-login", json={"email": "matnon@x.com"}).json()["access_token"]}
        r = c.post(f"/admin/concepts/{cid}/materials",
                   files={"file": ("x.pdf", b"%PDF x", "application/pdf")}, headers=L)
        assert r.status_code == 403


def test_material_download_entitlement_gate():
    original = settings.enforce_entitlements
    settings.enforce_entitlements = True
    try:
        with TestClient(app) as c:
            A = _admin(c, "mat-gate2@vettalume.test")
            cid = _make_concept(c, A)  # concept belongs to CAT
            mid = c.post(f"/admin/concepts/{cid}/materials",
                         files={"file": ("g.pdf", b"%PDF gated content here", "application/pdf")},
                         headers=A).json()["id"]
            # learner is granted GMAT (not CAT) -> blocked from a CAT material
            tok = c.post("/auth/dev-login", json={"email": "gateduser@x.com", "exam_code": "GMAT"}).json()["access_token"]
            L = {"Authorization": "Bearer " + tok}
            assert c.get(f"/learn/materials/{mid}/download", headers=L).status_code == 403
            # grant CAT via admin enroll -> now allowed
            sid = c.get("/admin/students", params={"q": "gateduser@x.com"}, headers=A).json()["students"][0]["id"]
            c.post(f"/admin/students/{sid}/enroll", json={"exam_code": "CAT"}, headers=A)
            assert c.get(f"/learn/materials/{mid}/download", headers=L).status_code == 200
    finally:
        settings.enforce_entitlements = original


def test_admin_material_download_bypasses_entitlement():
    """Admin preview route returns the file bytes even with enforcement on and no entitlement."""
    settings.enforce_entitlements = True
    try:
        with TestClient(app) as c:
            A = _admin(c, "mat-admin2@vettalume.test")
            cid = _make_concept(c, A)
            pdf = b"%PDF-1.4 " + b"y" * 500
            mid = c.post(f"/admin/concepts/{cid}/materials",
                         files={"file": ("sheet.pdf", pdf, "application/pdf")}, headers=A).json()["id"]
            r = c.get(f"/admin/materials/{mid}/download", headers=A)
            assert r.status_code == 200
            assert r.headers["content-type"] == "application/pdf"
            assert r.content[:5] == b"%PDF-"
            assert "inline" in r.headers.get("content-disposition", "")
    finally:
        settings.enforce_entitlements = False
