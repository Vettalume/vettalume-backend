"""Admin portal tests — the content perimeter and the authoring API.

Uses TestClient(app) (shared in-memory app DB within the process), so every test uses unique emails
and unique ids to stay independent.
"""
import os

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["SERVE_ONLY_APPROVED"] = "true"

from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402


def _admin(c, email):
    """Register an account whose email is in admin_emails -> it is an admin; return its auth header."""
    cur = {e.strip() for e in (settings.admin_emails or "").split(",") if e.strip()}
    cur.add(email)
    settings.admin_emails = ",".join(cur)
    r = c.post("/auth/register", json={"email": email, "password": "adminpass123"}).json()
    return {"Authorization": "Bearer " + r["access_token"]}


def test_perimeter_blocks_outsiders():
    with TestClient(app) as c:
        # unauthenticated -> 401 on read and write
        assert c.get("/admin/exams").status_code == 401
        assert c.post("/admin/topics", json={"id": "x", "exam_code": "CAT", "section_key": "QA", "name": "X"}).status_code == 401
        assert c.post("/ingest/items", json=[]).status_code == 401
        # authenticated NON-admin (a normal learner with a real JWT) -> 403
        tok = c.post("/auth/dev-login", json={"email": "outsider@x.com"}).json()["access_token"]
        H = {"Authorization": "Bearer " + tok}
        assert c.get("/admin/exams", headers=H).status_code == 403
        assert c.post("/ingest/items", json=[], headers=H).status_code == 403


def test_admin_can_build_syllabus_and_item_goes_live():
    with TestClient(app) as c:
        A = _admin(c, "syl-admin@vettalume.test")
        assert c.get("/admin/me", headers=A).json()["is_admin"] is True

        # build topic -> concept -> prereq under existing CAT/QA
        assert c.post("/admin/topics", json={"id": "z-tsd", "exam_code": "CAT", "section_key": "QA", "name": "TSD"}, headers=A).status_code == 200
        assert c.post("/admin/concepts", json={"id": "z-tsd-basic", "exam_code": "CAT", "section_key": "QA", "name": "TSD basics", "parent_id": "z-tsd"}, headers=A).status_code == 200
        assert c.post("/admin/prereqs", json={"node_id": "z-tsd-basic", "prereq_node_id": "avg-simple"}, headers=A).status_code == 200

        # the concept appears in the admin syllabus with its prereq
        syl = c.get("/admin/syllabus", params={"exam": "CAT"}, headers=A).json()
        node = next(n for n in syl["nodes"] if n["id"] == "z-tsd-basic")
        assert node["prereqs"] == ["avg-simple"]

        # create an item on the new concept, approve it, and confirm it serves live
        item = {"item_id": "z-tsd-1", "exam_code": "CAT", "section_key": "QA", "concept_node_id": "z-tsd-basic",
                "difficulty_d": 0, "format": "mcq", "options": ["10", "20", "30", "40"], "correct_answer": "20",
                "stem": "100 km in 5 h?", "solution": "20 km/h", "status": "draft"}
        assert c.post("/admin/items", json=item, headers=A).json()["status"] == "committed"
        assert c.post("/admin/items/z-tsd-1/approve", headers=A).json()["status"] == "approved"

        learner = c.post("/auth/dev-login", json={"email": "z-learner@x.com"}).json()
        LH = {"Authorization": "Bearer " + learner["access_token"]}
        nxt = c.get("/practice/next", params={"node_id": "z-tsd-basic"}, headers=LH).json()
        assert nxt["item_id"] == "z-tsd-1"
        assert "correct_answer" not in nxt  # learner view still never leaks the answer


def test_item_lifecycle_edit_retire_delete():
    with TestClient(app) as c:
        A = _admin(c, "life-admin@vettalume.test")
        item = {"item_id": "z-life-1", "exam_code": "CAT", "section_key": "QA", "concept_node_id": "avg-simple",
                "difficulty_d": 0, "format": "mcq", "options": ["6", "7"], "correct_answer": "6", "stem": "q", "status": "draft"}
        c.post("/admin/items", json=item, headers=A)
        # edit bumps version
        v = c.patch("/admin/items/z-life-1", json={"solution": "because"}, headers=A).json()
        assert v["version"] == 2
        # retire then delete
        assert c.post("/admin/items/z-life-1/retire", headers=A).json()["status"] == "retired"
        assert c.delete("/admin/items/z-life-1", headers=A).json()["deleted"] == "z-life-1"
        assert c.delete("/admin/items/z-life-1", headers=A).status_code == 404


def test_xlsx_upload_is_admin_only():
    with TestClient(app) as c:
        # no auth -> 401 (we don't even need a real file; the guard fires first)
        assert c.post("/admin/items/upload-xlsx").status_code == 401
        tok = c.post("/auth/dev-login", json={"email": "noadmin@x.com"}).json()["access_token"]
        assert c.post("/admin/items/upload-xlsx", headers={"Authorization": "Bearer " + tok}).status_code == 403


def test_grant_and_revoke_admin():
    with TestClient(app) as c:
        A = _admin(c, "owner@vettalume.test")
        # a plain registered account is not an admin yet
        c.post("/auth/register", json={"email": "teammate@vettalume.test", "password": "teampass12"})
        # grant -> appears in the list
        assert c.post("/admin/admins", json={"email": "teammate@vettalume.test"}, headers=A).json()["ok"] is True
        emails = [a["email"] for a in c.get("/admin/admins", headers=A).json()]
        assert "teammate@vettalume.test" in emails
        # the teammate can now reach admin endpoints
        tlog = c.post("/auth/login", json={"email": "teammate@vettalume.test", "password": "teampass12"}).json()
        TH = {"Authorization": "Bearer " + tlog["access_token"]}
        assert c.get("/admin/me", headers=TH).json()["is_admin"] is True
        # revoke -> teammate loses access
        acc_id = next(a["account_id"] for a in c.get("/admin/admins", headers=A).json() if a["email"] == "teammate@vettalume.test")
        assert c.delete("/admin/admins/" + acc_id, headers=A).json()["ok"] is True
        assert c.get("/admin/me", headers=TH).status_code == 403


def test_grant_nonexistent_email_404():
    with TestClient(app) as c:
        A = _admin(c, "owner2@vettalume.test")
        assert c.post("/admin/admins", json={"email": "ghost@nowhere.test"}, headers=A).status_code == 404


def _quiz_xlsx(rows):
    import io

    import openpyxl
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["Question", "Option A", "Option B", "Option C", "Option D", "Answer", "Difficulty", "Solution"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return buf


def _fresh_concept(c, A, cid):
    """Create a unique topic+concept under CAT/QA so tests don't pollute the seed graph."""
    c.post("/admin/topics", json={"id": cid + "-t", "exam_code": "CAT", "section_key": "QA", "name": cid + " topic"}, headers=A)
    c.post("/admin/concepts", json={"id": cid, "exam_code": "CAT", "section_key": "QA", "name": cid + " concept", "parent_id": cid + "-t"}, headers=A)


def test_concept_content_videos_and_student_sees_them():
    with TestClient(app) as c:
        A = _admin(c, "content-admin@vettalume.test")
        _fresh_concept(c, A, "z-cms-content")
        put = c.put("/admin/concepts/z-cms-content/content",
                    json={"body": "Average = sum / count.",
                          "videos": [{"title": "Averages", "url": "https://youtu.be/x", "seconds": 120}]},
                    headers=A)
        assert put.status_code == 200 and put.json()["videos"] == 1
        got = c.get("/admin/concepts/z-cms-content/content", headers=A).json()
        assert got["body"].startswith("Average") and len(got["videos"]) == 1
        # a student sees the content via the learner endpoint
        L = {"Authorization": "Bearer " + c.post("/auth/dev-login", json={"email": "cv-stud@x.com"}).json()["access_token"]}
        cd = c.get("/learn/concept/z-cms-content", headers=L).json()
        assert cd["content"]["body"].startswith("Average") and len(cd["content"]["videos"]) == 1


def test_subtopic_quiz_bulk_upload():
    with TestClient(app) as c:
        A = _admin(c, "quiz-admin@vettalume.test")
        _fresh_concept(c, A, "z-cms-quiz")
        buf = _quiz_xlsx([["Avg of 2,4,6?", "3", "4", "5", "6", "4", 0, "12/3=4"],
                          ["Avg of 10,20?", "10", "15", "20", "25", "15", 1, "30/2"]])
        up = c.post("/admin/concepts/z-cms-quiz/items/upload-xlsx",
                    files={"file": ("quiz.xlsx", buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                    headers=A)
        assert up.status_code == 200 and up.json()["inserted"] == 2
        items = c.get("/admin/items", params={"exam": "CAT", "concept": "z-cms-quiz"}, headers=A).json()
        assert items["count"] == 2


def test_mock_only_scope_excludes_from_practice():
    with TestClient(app) as c:
        A = _admin(c, "mock-admin@vettalume.test")
        _fresh_concept(c, A, "z-cms-mock")
        # one normal practice item + one mock_only item on the same concept
        c.post("/admin/concepts/z-cms-mock/items/upload-xlsx",
               files={"file": ("p.xlsx", _quiz_xlsx([["Practice Q?", "1", "2", "3", "4", "2", 0, "two"]]),
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}, headers=A)
        up = c.post("/admin/concepts/z-cms-mock/items/upload-xlsx?scope=mock_only",
                    files={"file": ("m.xlsx", _quiz_xlsx([["Mock-only Q?", "1", "2", "3", "4", "2", 0, "two"]]),
                                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}, headers=A)
        assert up.status_code == 200 and up.json()["inserted"] == 1
        mock_ids = [i["item_id"] for i in c.get("/admin/items", params={"exam": "CAT", "concept": "z-cms-mock"}, headers=A).json()["items"]
                    if i["stem"].startswith("Mock-only")]
        assert mock_ids
        # practice must never serve a mock_only item
        L = {"Authorization": "Bearer " + c.post("/auth/dev-login", json={"email": "mo-stud@x.com"}).json()["access_token"]}
        served = set()
        for _ in range(8):
            nxt = c.get("/practice/next", params={"node_id": "z-cms-mock"}, headers=L).json()
            if nxt.get("item_id"):
                served.add(nxt["item_id"])
                c.post("/practice/answer", json={"item_id": nxt["item_id"], "answer": "x"}, headers=L)
        assert not (set(mock_ids) & served)


def test_student_management_deregister_and_enroll():
    with TestClient(app) as c:
        A = _admin(c, "stu-admin@vettalume.test")
        c.post("/auth/dev-login", json={"email": "managed@x.com"})   # learner self-enrols in CAT
        students = c.get("/admin/students", params={"q": "managed@x.com"}, headers=A).json()
        assert students["count"] == 1
        sid = students["students"][0]["id"]
        assert any(co["exam"] == "CAT" for co in students["students"][0]["courses"])
        dr = c.post(f"/admin/students/{sid}/deregister", json={"exam_code": "CAT"}, headers=A)
        assert dr.status_code == 200 and dr.json()["deregistered"] == "CAT"
        assert not c.get(f"/admin/students/{sid}", headers=A).json()["courses"]
        en = c.post(f"/admin/students/{sid}/enroll", json={"exam_code": "GMAT"}, headers=A)
        assert en.status_code == 200
        assert any(co["exam"] == "GMAT" for co in c.get(f"/admin/students/{sid}", headers=A).json()["courses"])
        assert c.post(f"/admin/students/{sid}/deregister", json={"exam_code": "GRE"}, headers=A).status_code == 404
