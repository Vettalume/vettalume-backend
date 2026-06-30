"""Concept HTML upload (Phase 16): admin uploads an .html file -> stored sanitized -> served clean."""
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
    tid, cid = "thtml-" + _u.uuid4().hex[:6], "chtml-" + _u.uuid4().hex[:6]
    c.post("/admin/topics", json={"id": tid, "exam_code": "CAT", "section_key": "QA", "name": "T"}, headers=A)
    c.post("/admin/concepts", json={"id": cid, "exam_code": "CAT", "section_key": "QA",
                                    "name": "Quadratics", "parent_id": tid}, headers=A)
    return cid


def test_upload_html_is_sanitized_and_stored():
    with TestClient(app) as c:
        A = _admin(c, "html-admin@vettalume.test")
        cid = _make_concept(c, A)
        # give it a video first, to prove the upload preserves existing videos
        c.put(f"/admin/concepts/{cid}/content", headers=A,
              json={"body": "old", "videos": [{"title": "Intro", "url": "https://gumlet/v1"}]})

        html = (b"<html><head><title>x</title>"
                b"<style>.formula{color:#b8862f}body{background:url(javascript:bad)}</style>"
                b"</head><body>"
                b"<h2 class='formula'>Quadratic Equations</h2>"
                b"<p onclick='steal()'>Solve <b>x</b></p>"
                b"<script>alert(document.cookie)</script>"
                b"<img src='https://store/fig1.png' alt='fig' onerror='hack()'>"
                b"<a href='javascript:evil()'>bad</a>"
                b"</body></html>")
        up = c.post(f"/admin/concepts/{cid}/content/html", headers=A,
                    files={"file": ("concept.html", html, "text/html")})
        assert up.status_code == 200
        out = up.json()["body"]
        # dangerous stripped
        assert "<script" not in out and "alert(" not in out
        assert "onclick" not in out and "onerror" not in out and "javascript:" not in out
        assert "<title" not in out
        # style block KEPT and the colour preserved (only dangerous CSS neutralised)
        assert "<style>" in out and "#b8862f" in out
        # safe content kept, classes preserved (so the CSS targets it)
        assert "<h2" in out and 'class="formula"' in out and "<b>" in out
        assert 'src="https://store/fig1.png"' in out

        # persisted + video preserved
        got = c.get(f"/admin/concepts/{cid}/content", headers=A).json()
        assert "<h2" in got["body"] and "<style>" in got["body"] and "<script" not in got["body"]
        assert len(got["videos"]) == 1 and got["videos"][0]["url"] == "https://gumlet/v1"


def test_paste_save_also_sanitizes():
    with TestClient(app) as c:
        A = _admin(c, "html-admin2@vettalume.test")
        cid = _make_concept(c, A)
        c.put(f"/admin/concepts/{cid}/content", headers=A,
              json={"body": "<p>hi</p><script>bad()</script>", "videos": []})
        got = c.get(f"/admin/concepts/{cid}/content", headers=A).json()
        assert "<p>hi</p>" in got["body"] and "<script" not in got["body"]
