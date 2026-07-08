"""Diagnostic test flow (Phase 18).

An admin authors a Mock of type='diagnostic'; a learner takes it ONCE, which grades each section and
writes a per-section ability (AbilityEstimate scope='diagnostic:<section>'). Covers: admin authoring,
the paper hides answers, scoring per section, persisted ability, and the once-only gate (409 on retake).
"""
import os

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["SERVE_ONLY_APPROVED"] = "true"

from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402


def _admin(c, email):
    cur = {e.strip() for e in (settings.admin_emails or "").split(",") if e.strip()}
    cur.add(email)
    settings.admin_emails = ",".join(cur)
    r = c.post("/auth/register", json={"email": email, "password": "adminpass123"}).json()
    return {"Authorization": "Bearer " + r["access_token"]}


def _learner(c, email):
    return {"Authorization": "Bearer " + c.post("/auth/dev-login", json={"email": email}).json()["access_token"]}


def _make_diagnostic(c, A, exam="CAT"):
    """Admin authors + publishes a 2-section diagnostic; returns its id."""
    mid = c.post("/admin/mocks", json={"type": "diagnostic", "name": "CAT Diagnostic",
                                       "exam": exam, "duration": 60}, headers=A).json()["id"]
    structure = {"sections": [
        {"id": "s-qa", "name": "QA", "time": 30, "questions": [
            {"id": "qa1", "text": "2+2?", "options": ["3", "4", "5", "6"], "correct": 1, "difficulty": -1, "solution": "4", "image": ""},
            {"id": "qa2", "text": "3*3?", "options": ["6", "9", "12", "8"], "correct": 1, "difficulty": 0, "solution": "9", "image": ""},
            {"id": "qa3", "text": "sqrt(81)?", "options": ["7", "8", "9", "6"], "correct": 2, "difficulty": 1, "solution": "9", "image": ""},
        ]},
        {"id": "s-varc", "name": "VARC", "time": 30, "questions": [
            {"id": "v1", "text": "Synonym of big?", "options": ["small", "large", "thin", "old"], "correct": 1, "difficulty": 0, "solution": "large", "image": ""},
            {"id": "v2", "text": "Antonym of hot?", "options": ["warm", "cold", "mild", "dry"], "correct": 1, "difficulty": 1, "solution": "cold", "image": ""},
        ]},
    ]}
    c.put(f"/admin/mocks/{mid}/structure", json=structure, headers=A)
    assert c.post(f"/admin/mocks/{mid}/publish", headers=A).json()["status"] == "published"
    return mid


def test_admin_can_author_a_diagnostic_type_mock():
    with TestClient(app) as c:
        A = _admin(c, "diag-admin1@vettalume.test")
        m = c.post("/admin/mocks", json={"type": "diagnostic", "name": "D", "exam": "CAT"}, headers=A)
        assert m.status_code == 200 and m.json()["type"] == "diagnostic"
        # built with all CAT sections, exactly like a full mock
        assert {s["name"] for s in m.json()["sections"]} == {"DILR", "QA", "VARC"}


def test_status_not_configured_until_published():
    with TestClient(app) as c:
        _admin(c, "diag-admin2@vettalume.test")
        L = _learner(c, "stud-nc@x.com")
        st = c.get("/diagnostic/status", params={"exam": "CAT"}, headers=L).json()
        assert st["state"] == "not_configured"
        # cannot start what isn't configured
        assert c.post("/diagnostic/start", params={"exam": "CAT"}, headers=L).status_code == 404


def test_full_flow_scores_each_section_and_locks_after_one_attempt():
    with TestClient(app) as c:
        A = _admin(c, "diag-admin3@vettalume.test")
        _make_diagnostic(c, A, exam="CAT")
        L = _learner(c, "stud-flow@x.com")

        # available before taking
        assert c.get("/diagnostic/status", params={"exam": "CAT"}, headers=L).json()["state"] == "available"

        # start -> paper carries the questions but NOT the correct answers or solutions
        paper = c.post("/diagnostic/start", params={"exam": "CAT"}, headers=L).json()
        assert paper["total_questions"] == 5 and len(paper["sections"]) == 2
        qa = next(s for s in paper["sections"] if s["name"] == "QA")
        assert "correct" not in qa["questions"][0] and "solution" not in qa["questions"][0]
        # now in progress
        assert c.get("/diagnostic/status", params={"exam": "CAT"}, headers=L).json()["state"] == "in_progress"

        # submit: QA fully correct (strong), VARC fully wrong (weak)
        answers = {"qa1": 1, "qa2": 1, "qa3": 2, "v1": 0, "v2": 0}
        body = c.post("/diagnostic/submit", params={"exam": "CAT"}, json={"answers": answers}, headers=L).json()
        assert body["state"] == "completed"
        assert body["sections"]["QA"]["raw"] == 3 and body["sections"]["QA"]["total"] == 3
        assert body["sections"]["VARC"]["raw"] == 0 and body["sections"]["VARC"]["total"] == 2
        # the diagnostic SET each section's ability, and the strong section sits above the weak one
        assert body["sections"]["QA"]["theta"] > body["sections"]["VARC"]["theta"]
        # ability is a band, never a bare point (the honesty contract)
        assert len(body["sections"]["QA"]["band_95"]) == 2

        # per-section ability persisted as AbilityEstimate scope='diagnostic:<section>'
        db = SessionLocal()
        try:
            acc = db.query(models.Account).filter(models.Account.email == "stud-flow@x.com").one()
            scopes = {a.scope for a in db.query(models.AbilityEstimate).filter(
                models.AbilityEstimate.learner_id == acc.id).all()}
        finally:
            db.close()
        assert "diagnostic:QA" in scopes and "diagnostic:VARC" in scopes and "diagnostic" in scopes

        # ---- the once-only gate ----
        again = c.post("/diagnostic/submit", params={"exam": "CAT"}, json={"answers": answers}, headers=L)
        assert again.status_code == 409
        assert c.post("/diagnostic/start", params={"exam": "CAT"}, headers=L).status_code == 409
        assert c.get("/diagnostic/status", params={"exam": "CAT"}, headers=L).json()["state"] == "completed"

        # result returns the per-section ability of the completed attempt
        rez = c.get("/diagnostic/result", params={"exam": "CAT"}, headers=L).json()
        assert rez["state"] == "completed" and set(rez["sections"]) == {"QA", "VARC"}


def test_two_learners_each_get_their_own_one_attempt():
    with TestClient(app) as c:
        A = _admin(c, "diag-admin4@vettalume.test")
        _make_diagnostic(c, A, exam="CAT")
        L1 = _learner(c, "stud-a@x.com")
        L2 = _learner(c, "stud-b@x.com")
        # learner 1 takes it
        c.post("/diagnostic/start", params={"exam": "CAT"}, headers=L1)
        c.post("/diagnostic/submit", params={"exam": "CAT"}, json={"answers": {"qa1": 1}}, headers=L1)
        assert c.get("/diagnostic/status", params={"exam": "CAT"}, headers=L1).json()["state"] == "completed"
        # learner 2 is unaffected — still available
        assert c.get("/diagnostic/status", params={"exam": "CAT"}, headers=L2).json()["state"] == "available"
