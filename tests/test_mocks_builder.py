"""Mock-builder tests — admin-authored fixed-form mocks (sectional + full): create, list,
configure, persist the section/question structure, publish, delete, and admin gating."""
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


def test_create_sectional_and_full():
    with TestClient(app) as c:
        A = _admin(c, "mock-admin1@vettalume.test")
        # sectional -> seeded with one section, draft
        sec = c.post("/admin/mocks", json={"type": "sectional", "name": "QA Sectional 1",
                                           "exam": "CAT", "negative": 1}, headers=A)
        assert sec.status_code == 200
        sm = sec.json()
        assert sm["type"] == "sectional" and sm["status"] == "draft" and sm["negative"] == 1
        assert len(sm["sections"]) == 1 and "questions" in sm["sections"][0]

        # full -> seeded with all of CAT's sections (DILR, QA, VARC)
        full = c.post("/admin/mocks", json={"type": "full", "name": "Full Mock 1", "exam": "CAT",
                                            "duration": 120, "scoringMarks": 3, "scoringNeg": 1,
                                            "instructions": "Three sections."}, headers=A).json()
        assert full["type"] == "full" and full["duration"] == 120 and full["scoringMarks"] == 3
        assert {s["name"] for s in full["sections"]} == {"DILR", "QA", "VARC"}


def test_list_filter_and_config_and_publish_delete():
    with TestClient(app) as c:
        A = _admin(c, "mock-admin2@vettalume.test")
        s = c.post("/admin/mocks", json={"type": "sectional", "name": "S", "exam": "GMAT"}, headers=A).json()
        c.post("/admin/mocks", json={"type": "full", "name": "F", "exam": "GMAT"}, headers=A)
        mid = s["id"]

        # list all for the exam, and filter by type
        assert c.get("/admin/mocks", params={"exam": "GMAT"}, headers=A).json()["count"] == 2
        only_sec = c.get("/admin/mocks", params={"exam": "GMAT", "type": "sectional"}, headers=A).json()
        assert only_sec["count"] == 1 and only_sec["mocks"][0]["type"] == "sectional"

        # update config
        upd = c.put(f"/admin/mocks/{mid}", json={"name": "S renamed", "negative": 2}, headers=A).json()
        assert upd["name"] == "S renamed" and upd["negative"] == 2

        # publish toggles
        assert c.post(f"/admin/mocks/{mid}/publish", headers=A).json()["status"] == "published"
        assert c.post(f"/admin/mocks/{mid}/publish", headers=A).json()["status"] == "draft"

        # delete
        assert c.delete(f"/admin/mocks/{mid}", headers=A).json()["ok"] is True
        assert c.get("/admin/mocks", params={"exam": "GMAT"}, headers=A).json()["count"] == 1


def test_mock_structure_persists():
    with TestClient(app) as c:
        A = _admin(c, "mock-admin3@vettalume.test")
        mid = c.post("/admin/mocks", json={"type": "sectional", "name": "Struct", "exam": "CAT"}, headers=A).json()["id"]
        structure = {"sections": [
            {"id": "sx", "name": "QA", "time": 40, "numQuestions": 2, "questions": [
                {"id": "q1", "text": "2+2?", "options": ["3", "4", "5", "6"], "correct": 1, "difficulty": 0, "solution": "It's 4.", "image": ""},
                {"id": "q2", "text": "Avg of 4,6?", "options": ["4", "5", "6", "7"], "correct": 1, "difficulty": -1, "solution": "5.", "image": ""},
            ]},
            {"id": "sy", "name": "VARC", "time": 40, "numQuestions": 0, "questions": []},
        ]}
        out = c.put(f"/admin/mocks/{mid}/structure", json=structure, headers=A).json()
        assert out["totalQuestions"] == 2 and len(out["sections"]) == 2
        # round-trips on re-fetch, question content preserved
        got = c.get(f"/admin/mocks/{mid}", headers=A).json()
        qa = next(s for s in got["sections"] if s["name"] == "QA")
        assert len(qa["questions"]) == 2 and qa["questions"][0]["text"] == "2+2?"
        assert qa["questions"][0]["correct"] == 1 and qa["questions"][0]["options"][1] == "4"


def test_mock_builder_requires_admin():
    with TestClient(app) as c:
        _admin(c, "mock-gate@vettalume.test")
        L = {"Authorization": "Bearer " + c.post("/auth/dev-login", json={"email": "mocknon@x.com"}).json()["access_token"]}
        assert c.get("/admin/mocks", params={"exam": "CAT"}, headers=L).status_code == 403
        assert c.post("/admin/mocks", json={"type": "full", "name": "X", "exam": "CAT"}, headers=L).status_code == 403
