"""Diagnostic test flow (Phase 18).

A diagnostic is an admin-authored Mock (type='diagnostic') a learner takes ONCE per exam. On submit it
grades each section and writes a per-section ability — stored as an AbilityEstimate with
scope='diagnostic:<section>' — so every section gets a baseline the rest of the engine builds on.
This is distinct from services/diagnosis.py, which is the cause/leak ANALYSIS over the practice loop.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select

from .. import models
from . import irt
from . import media


def active_diagnostic(db, exam: str):
    """The published diagnostic paper for an exam (latest wins). None if the admin hasn't set one."""
    return (db.query(models.Mock)
            .filter(models.Mock.exam_code == exam,
                    models.Mock.type == "diagnostic",
                    models.Mock.status == "published")
            .order_by(models.Mock.created_at.desc())
            .first())


def _attempt(db, learner, exam: str):
    return db.scalar(select(models.DiagnosticAttempt).where(
        models.DiagnosticAttempt.learner_id == learner.id,
        models.DiagnosticAttempt.exam_code == exam))


def status(db, learner, exam: str) -> dict:
    exam = (exam or "").upper()
    mock = active_diagnostic(db, exam)
    att = _attempt(db, learner, exam)
    if att is not None and att.status == "completed":
        state = "completed"
    elif mock is None:
        state = "not_configured"
    elif att is not None and att.status == "in_progress":
        state = "in_progress"
    else:
        state = "available"
    return {"exam": exam, "state": state,
            "diagnostic_id": mock.id if mock else None,
            "name": mock.name if mock else None,
            "completed_at": att.completed_at.isoformat() if (att and att.completed_at) else None}


def _paper(db, mock) -> dict:
    """The paper to present — sections + questions WITHOUT correct answers or solutions."""
    all_ids = [c for s in (mock.sections or []) for q in s.get("questions", [])
               for c in (q.get("id"), q.get("externalId"))]
    img_keys = media.existing_keys(db, all_ids)
    secs = []
    for s in (mock.sections or []):
        qs = [{"id": q.get("id"), "text": q.get("text", ""), "options": q.get("options", []),
               "image": media.resolve(q.get("image", ""), [q.get("id"), q.get("externalId")], img_keys),
               "difficulty": q.get("difficulty", 0)}
              for q in s.get("questions", [])]
        secs.append({"id": s.get("id"), "name": s.get("name"), "time": s.get("time", 0), "questions": qs})
    return {"diagnostic_id": mock.id, "name": mock.name, "exam": mock.exam_code,
            "duration": mock.duration, "instructions": mock.instructions or "", "negative": mock.negative,
            "sections": secs, "total_questions": sum(len(s["questions"]) for s in secs)}


def start(db, learner, exam: str) -> dict:
    exam = (exam or "").upper()
    mock = active_diagnostic(db, exam)
    if mock is None:
        raise HTTPException(404, {"error": "no_diagnostic",
                                  "detail": "No diagnostic test is set up for this exam yet."})
    att = _attempt(db, learner, exam)
    if att is not None and att.status == "completed":
        raise HTTPException(409, {"error": "already_taken",
                                  "detail": "You have already taken your diagnostic test."})
    if att is None:
        db.add(models.DiagnosticAttempt(learner_id=learner.id, exam_code=exam,
                                        mock_id=mock.id, status="in_progress"))
        db.commit()
    return _paper(db, mock)


def _score_section(questions, answers) -> dict:
    """EAP ability for one section from the learner's answers. difficulty(-2..2) maps to IRT b; a=1,
    c=1/options (MCQ guessing). Returns theta, se, a 95% band, and the raw correct/total."""
    triples, raw, total = [], 0, 0
    for q in questions:
        total += 1
        sel = answers.get(str(q.get("id")))
        correct = sel is not None and str(sel).isdigit() and int(sel) == int(q.get("correct", -1))
        if correct:
            raw += 1
        b = max(-3.0, min(3.0, float(q.get("difficulty", 0) or 0)))
        nopt = len(q.get("options", []) or []) or 4
        triples.append((1.0, b, 1.0 / nopt, 1 if correct else 0))
    theta, se = irt.eap_ability(triples) if triples else (0.0, 1.0)
    return {"theta": round(theta, 4), "se": round(se, 4),
            "band_95": [round(theta - 2 * se, 3), round(theta + 2 * se, 3)],
            "raw": raw, "total": total, "n_items": len(triples)}


def submit(db, learner, exam: str, answers: dict) -> dict:
    exam = (exam or "").upper()
    mock = active_diagnostic(db, exam)
    att = _attempt(db, learner, exam)
    if att is not None and att.status == "completed":
        raise HTTPException(409, {"error": "already_taken",
                                  "detail": "You have already taken your diagnostic test."})
    if mock is None:
        raise HTTPException(404, {"error": "no_diagnostic"})
    if att is None:
        att = models.DiagnosticAttempt(learner_id=learner.id, exam_code=exam,
                                       mock_id=mock.id, status="in_progress")
        db.add(att)
    answers = {str(k): v for k, v in (answers or {}).items()}

    per_section = {}
    for s in (mock.sections or []):
        key = s.get("name") or s.get("id")
        sc = _score_section(s.get("questions", []), answers)
        per_section[key] = sc
        db.add(models.AbilityEstimate(learner_id=learner.id, exam_code=exam,
                                      scope=f"diagnostic:{key}", theta=sc["theta"], se=sc["se"],
                                      n_items=sc["n_items"], method="eap"))
    all_q = [q for s in (mock.sections or []) for q in s.get("questions", [])]
    overall = _score_section(all_q, answers)
    db.add(models.AbilityEstimate(learner_id=learner.id, exam_code=exam, scope="diagnostic",
                                  theta=overall["theta"], se=overall["se"],
                                  n_items=overall["n_items"], method="eap"))

    now = datetime.utcnow()
    att.answers, att.section_ability = answers, per_section
    att.status, att.completed_at = "completed", now
    db.commit()
    return {"exam": exam, "state": "completed", "completed_at": now.isoformat(),
            "overall": overall, "sections": per_section}


def result(db, learner, exam: str) -> dict:
    exam = (exam or "").upper()
    att = _attempt(db, learner, exam)
    if att is None or att.status != "completed":
        raise HTTPException(404, {"error": "not_completed",
                                  "detail": "No completed diagnostic for this exam."})
    return {"exam": exam, "state": "completed", "sections": att.section_ability,
            "completed_at": att.completed_at.isoformat() if att.completed_at else None}
