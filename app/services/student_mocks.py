"""Student-facing mock catalog + taking flow for ADMIN-AUTHORED mocks.

An admin authors a fixed-form Mock (type='sectional' | 'full') in the content portal — sections with
embedded questions ({id, text, options, image, difficulty, correct}). This service is the student
side: list the PUBLISHED ones, serve a paper WITHOUT the answer key, and grade a submission.

Distinct from services/mock_session.py (the adaptive IRT engine that builds a paper from the item
bank) and services/diagnostic.py (the once-only diagnostic). Mirrors the diagnostic's paper/grade
shape so the delivery format is consistent. Detailed post-mock analysis is a later phase; submit
returns the raw score so the taker gets immediate feedback.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select

from .. import models

# The mock "types" a student browses. 'diagnostic' has its own once-only flow (services/diagnostic).
STUDENT_MOCK_TYPES = ("sectional", "full")


def _summary(m: models.Mock) -> dict:
    """Listing card: enough to render a mock tile without shipping the questions."""
    secs = m.sections or []
    tq = sum(len(s.get("questions", []) or []) for s in secs)
    tm = sum(int(s.get("time", 0) or 0) for s in secs)
    return {
        "id": m.id, "exam": m.exam_code, "type": m.type, "name": m.name,
        "duration": m.duration, "negative": m.negative,
        "scoringMarks": m.scoring_marks, "scoringNeg": m.scoring_neg,
        "instructions": m.instructions or "",
        "sections": [{"id": s.get("id"), "name": s.get("name"), "time": s.get("time", 0),
                      "questionCount": len(s.get("questions", []) or [])} for s in secs],
        "totalQuestions": tq, "totalTime": tm,
    }


def list_published(db, exam: str, mock_type: str | None = None) -> dict:
    """Published sectional/full mocks for an exam (oldest first). Empty until an admin publishes one."""
    q = select(models.Mock).where(models.Mock.exam_code == exam,
                                   models.Mock.status == "published")
    if mock_type:
        q = q.where(models.Mock.type == mock_type)
    rows = db.scalars(q.order_by(models.Mock.created_at)).all()
    ms = [m for m in rows if m.type in STUDENT_MOCK_TYPES]
    return {"exam": exam, "type": mock_type, "count": len(ms), "mocks": [_summary(m) for m in ms]}


def _published_or_404(db, mid: str) -> models.Mock:
    m = db.get(models.Mock, mid)
    if m is None or m.status != "published" or m.type not in STUDENT_MOCK_TYPES:
        raise HTTPException(404, {"error": "no_mock", "detail": "No such published mock."})
    return m


def paper(db, mid: str) -> dict:
    """The mock to take — sections + questions WITHOUT the correct answers or solutions."""
    m = _published_or_404(db, mid)
    secs = []
    for s in (m.sections or []):
        qs = [{"id": q.get("id"), "text": q.get("text", ""), "options": q.get("options", []) or [],
               "image": q.get("image", ""), "difficulty": q.get("difficulty", 0),
               "format": q.get("format", "mcq")}
              for q in (s.get("questions", []) or [])]
        secs.append({"id": s.get("id"), "name": s.get("name"), "time": s.get("time", 0), "questions": qs})
    return {"id": m.id, "name": m.name, "type": m.type, "exam": m.exam_code,
            "duration": m.duration, "instructions": m.instructions or "", "negative": m.negative,
            "scoringMarks": m.scoring_marks, "scoringNeg": m.scoring_neg,
            "sections": secs, "totalQuestions": sum(len(s["questions"]) for s in secs)}


def _grade_section(questions, answers, marks_correct: float, marks_neg: float) -> dict:
    """Raw correct/attempted/total + marks for one section. `answers` maps question_id -> selected
    option index (as sent by the runner). Unanswered questions are skipped (no negative)."""
    raw = total = attempted = 0
    score = 0.0
    for q in questions:
        total += 1
        sel = answers.get(str(q.get("id")))
        if sel is None or str(sel).strip() == "":
            continue
        attempted += 1
        correct = str(sel).isdigit() and int(sel) == int(q.get("correct", -1))
        if correct:
            raw += 1
            score += marks_correct
        else:
            score -= marks_neg
    return {"raw": raw, "total": total, "attempted": attempted,
            "accuracy": round(raw / attempted, 4) if attempted else 0.0,
            "score": round(score, 2)}


def submit(db, learner, mid: str, answers: dict) -> dict:
    """Grade a submission and return the score. No persistence yet — the detailed post-mock analysis
    (and its storage) is a later phase; this gives the taker their raw result immediately."""
    m = _published_or_404(db, mid)
    answers = {str(k): v for k, v in (answers or {}).items()}
    # Full mocks carry their own per-correct / per-wrong marks; sectional uses 1 mark and `negative`.
    if m.type == "full":
        mc, mn = float(m.scoring_marks or 1), float(m.scoring_neg or 0)
    else:
        mc, mn = 1.0, float(m.negative or 0)

    per, tot_raw, tot_total, tot_att, tot_score = [], 0, 0, 0, 0.0
    for s in (m.sections or []):
        sc = _grade_section(s.get("questions", []) or [], answers, mc, mn)
        per.append({"name": s.get("name") or s.get("id"), **sc})
        tot_raw += sc["raw"]; tot_total += sc["total"]; tot_att += sc["attempted"]; tot_score += sc["score"]
    overall = {"raw": tot_raw, "total": tot_total, "attempted": tot_att,
               "accuracy": round(tot_raw / tot_att, 4) if tot_att else 0.0,
               "score": round(tot_score, 2)}
    return {"id": m.id, "name": m.name, "type": m.type, "sections": per, "overall": overall}
