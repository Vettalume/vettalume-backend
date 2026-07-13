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


def _question_passage(q) -> str:
    """The passage/caselet shown on the left in VARC/DILR. Newer imports store it in `passage`; some
    older imports dumped the passage text into `passageId` — a slug has no spaces, so a spaced value
    there is really the passage text (backwards-compatible fallback)."""
    p = (q.get("passage") or "").strip()
    if p:
        return p
    pid = str(q.get("passageId") or "")
    return pid if " " in pid else ""


def paper(db, mid: str) -> dict:
    """The mock to take — sections + questions WITHOUT the correct answers or solutions."""
    m = _published_or_404(db, mid)
    secs = []
    for s in (m.sections or []):
        qs = [{"id": q.get("id"), "text": q.get("text", ""), "options": q.get("options", []) or [],
               "image": q.get("image", ""), "difficulty": q.get("difficulty", 0),
               "format": q.get("format", "mcq"), "passage": _question_passage(q)}
              for q in (s.get("questions", []) or [])]
        secs.append({"id": s.get("id"), "name": s.get("name"), "time": s.get("time", 0), "questions": qs})
    return {"id": m.id, "name": m.name, "type": m.type, "exam": m.exam_code,
            "duration": m.duration, "instructions": m.instructions or "", "negative": m.negative,
            "scoringMarks": m.scoring_marks, "scoringNeg": m.scoring_neg,
            "sections": secs, "totalQuestions": sum(len(s["questions"]) for s in secs)}


def _is_tita(q) -> bool:
    """A question is type-in-the-answer (no options, no guessing floor) if it declares format='tita'
    or ships no options. TITA is graded on value equality and never carries negative marking."""
    return (q.get("format") == "tita") or not (q.get("options") or [])


def _is_correct(q, sel) -> bool:
    if _is_tita(q):
        return str(sel).strip().lower() == str(q.get("correct", "")).strip().lower()
    return str(sel).isdigit() and int(sel) == int(q.get("correct", -1))


def _marks_for(m: models.Mock) -> tuple[float, float]:
    """(marks_per_correct, negative_per_wrong_mcq). CAT uses the official +3 / −1 pattern; other
    exams use the marks the admin configured (full carries its own, sectional uses `negative`)."""
    if (m.exam_code or "").upper() == "CAT":
        return 3.0, 1.0
    if m.type == "full":
        return float(m.scoring_marks or 1), float(m.scoring_neg or 0)
    return 1.0, float(m.negative or 0)


def _grade_section(questions, answers, marks_correct: float, marks_neg: float) -> dict:
    """Correct/wrong/unattempted + marks for one section. `answers` maps question_id -> the selected
    option index (MCQ) or typed value (TITA). Unattempted questions are skipped (no negative); TITA
    wrong answers never take a negative."""
    raw = wrong = attempted = 0
    score = marks_total = 0.0
    total = len(questions)
    for q in questions:
        marks_total += marks_correct
        sel = answers.get(str(q.get("id")))
        if sel is None or str(sel).strip() == "":
            continue
        attempted += 1
        if _is_correct(q, sel):
            raw += 1
            score += marks_correct
        else:
            wrong += 1
            if not _is_tita(q):
                score -= marks_neg
    return {"raw": raw, "wrong": wrong, "unattempted": total - attempted, "total": total,
            "attempted": attempted, "accuracy": round(raw / attempted, 4) if attempted else 0.0,
            "score": round(score, 2), "marks_total": round(marks_total, 2)}


def _primary_section_key(m: models.Mock) -> str | None:
    """The section a sectional mock belongs to = its first section's name (QA/VARC/DILR). Full mocks
    span every section, so they have no single section key."""
    if m.type != "sectional":
        return None
    for s in (m.sections or []):
        name = (s.get("name") or "").strip()
        if name:
            return name.upper()
    return None


def submit(db, learner, mid: str, answers: dict, durations: dict | None = None,
           time_ms: int = 0) -> dict:
    """Grade a submission, PERSIST it as a MockAttempt, and return the score plus the attempt id
    (so the runner can link straight to that attempt's analysis)."""
    m = _published_or_404(db, mid)
    answers = {str(k): v for k, v in (answers or {}).items()}
    durations = {str(k): int(v or 0) for k, v in (durations or {}).items()}
    mc, mn = _marks_for(m)

    per = []
    tot = {"raw": 0, "wrong": 0, "unattempted": 0, "total": 0, "attempted": 0,
           "score": 0.0, "marks_total": 0.0}
    for s in (m.sections or []):
        sc = _grade_section(s.get("questions", []) or [], answers, mc, mn)
        per.append({"name": s.get("name") or s.get("id"), **sc})
        for k in tot:
            tot[k] += sc[k]
    overall = {**tot,
               "accuracy": round(tot["raw"] / tot["attempted"], 4) if tot["attempted"] else 0.0,
               "score": round(tot["score"], 2), "marks_total": round(tot["marks_total"], 2)}

    attempt = models.MockAttempt(
        learner_id=learner.id, mock_id=m.id, exam_code=m.exam_code, mock_type=m.type,
        section_key=_primary_section_key(m), mock_name=m.name,
        answers=answers, durations=durations, section_scores=per, overall=overall,
        time_ms=int(time_ms or sum(durations.values())),
    )
    db.add(attempt)
    db.commit()
    return {"attemptId": str(attempt.id), "id": m.id, "name": m.name, "type": m.type,
            "sections": per, "overall": overall}


def _band(d) -> str:
    """Authored difficulty -2..2 -> D1..D5."""
    try:
        return f"D{max(1, min(5, int(d) + 3))}"
    except (TypeError, ValueError):
        return "D3"


def individual_analysis(db, learner, attempt_id) -> dict:
    """One completed attempt in full: per-question review (your answer vs correct, right/wrong/skipped,
    time, solution), section scores, overall, and accuracy by difficulty band."""
    a = db.get(models.MockAttempt, attempt_id)
    if a is None or a.learner_id != learner.id:
        raise HTTPException(404, {"error": "no_attempt", "detail": "No such attempt."})
    m = db.get(models.Mock, a.mock_id)

    sections, questions = [], []
    band_tot: dict[str, int] = {}
    band_cor: dict[str, int] = {}
    topic_stats: dict[str, dict] = {}   # per-subtopic (from the Excel) accuracy -> strong/weak
    for s in ((m.sections if m else None) or []):
        qlist = []
        for q in (s.get("questions", []) or []):
            qid = str(q.get("id"))
            sel = a.answers.get(qid)
            attempted = not (sel is None or str(sel).strip() == "")
            correct = attempted and _is_correct(q, sel)
            result = "correct" if correct else ("wrong" if attempted else "skipped")
            b = _band(q.get("difficulty", 0))
            band_tot[b] = band_tot.get(b, 0) + 1
            band_cor[b] = band_cor.get(b, 0) + (1 if correct else 0)
            tname = str(q.get("subtopic") or q.get("topic") or "").strip()
            if tname:
                ts = topic_stats.setdefault(
                    tname, {"section": s.get("name"), "attempted": 0, "correct": 0, "total": 0})
                ts["total"] += 1
                if attempted:
                    ts["attempted"] += 1
                if correct:
                    ts["correct"] += 1
            opts = q.get("options", []) or []
            your_txt = (opts[int(sel)] if (opts and str(sel).isdigit() and 0 <= int(sel) < len(opts))
                        else (str(sel) if attempted else ""))
            corr_txt = (opts[int(q.get("correct"))] if (opts and str(q.get("correct")).isdigit()
                        and 0 <= int(q.get("correct")) < len(opts)) else str(q.get("correct", "")))
            entry = {
                "id": qid, "section": s.get("name"), "text": q.get("text", ""), "options": opts,
                "your_answer": your_txt, "correct_answer": corr_txt, "result": result,
                "difficulty": _band(q.get("difficulty", 0)), "solution": q.get("solution", "") or "",
                "time_ms": int(a.durations.get(qid, 0) or 0),
                "benchmark_s": int(q.get("time", 0) or 0) or None,
            }
            qlist.append(entry)
            questions.append(entry)
        sections.append({"name": s.get("name"), **next(
            (ss for ss in (a.section_scores or []) if ss.get("name") == s.get("name")), {}),
            "questions": qlist})

    difficulty_spread = [{
        "band": b, "answered": band_tot.get(b, 0), "correct": band_cor.get(b, 0),
        "cleared_pct": round(band_cor.get(b, 0) / band_tot[b], 4) if band_tot.get(b) else 0.0,
    } for b in ("D1", "D2", "D3", "D4", "D5")]

    # ---- subtopic-wise strong / weak + recommendations (from the questions' subtopic tags) ----
    # `score` = correct / TOTAL questions in the subtopic (coverage-aware): leaving questions
    # unattempted counts against you, so a 1-of-4 subtopic is weak even if that one was right.
    topics = [{
        "name": n, "section": v["section"], "attempted": v["attempted"],
        "correct": v["correct"], "total": v["total"],
        "accuracy": round(v["correct"] / v["attempted"], 4) if v["attempted"] else 0.0,
        "score": round(v["correct"] / v["total"], 4) if v["total"] else 0.0,
    } for n, v in topic_stats.items()]
    strong = sorted([t for t in topics if t["score"] >= 0.7],
                    key=lambda t: t["score"], reverse=True)[:5]
    weak = sorted([t for t in topics if t["score"] < 0.7], key=lambda t: t["score"])[:5]
    recommendations = [{
        "name": t["name"], "section": t["section"], "accuracy": t["score"],
        "tip": f"{round(t['score'] * 100)}% of {t['name']} solved — attempt and master the rest "
               f"before your next mock.",
    } for t in weak]

    return {
        "attemptId": str(a.id), "mockId": a.mock_id, "mockName": a.mock_name,
        "exam": a.exam_code, "type": a.mock_type, "section": a.section_key,
        "completedAt": a.created_at.isoformat() if a.created_at else None,
        "timeMs": a.time_ms, "overall": a.overall, "sections": sections,
        "difficulty_spread": difficulty_spread, "questions": questions,
        "topics": topics, "strong": strong, "weak": weak, "recommendations": recommendations,
    }


def section_analysis(db, learner, exam: str, section: str) -> dict:
    """Aggregate across every sectional-mock attempt the learner has made in one section: attempts
    count, latest/best score, average accuracy, and score/accuracy/time trends over time."""
    exam = (exam or "").upper()
    section = (section or "").upper()
    attempts = db.scalars(
        select(models.MockAttempt).where(
            models.MockAttempt.learner_id == learner.id,
            models.MockAttempt.exam_code == exam,
            models.MockAttempt.mock_type == "sectional",
            models.MockAttempt.section_key == section,
        ).order_by(models.MockAttempt.created_at)
    ).all()

    rows = []
    for a in attempts:
        ov = a.overall or {}
        n_q = ov.get("total", 0) or 0
        rows.append({
            "attemptId": str(a.id), "mockId": a.mock_id, "mockName": a.mock_name,
            "score": ov.get("score", 0), "marksTotal": ov.get("marks_total", 0),
            "raw": ov.get("raw", 0), "total": n_q, "attempted": ov.get("attempted", 0),
            "accuracy": ov.get("accuracy", 0.0),
            "avgTimePerQ": round((a.time_ms / 1000) / n_q, 1) if n_q else 0.0,
            "completedAt": a.created_at.isoformat() if a.created_at else None,
        })

    scores = [r["score"] for r in rows]
    accs = [r["accuracy"] for r in rows]
    published = db.scalars(
        select(models.Mock).where(models.Mock.exam_code == exam,
                                  models.Mock.type == "sectional",
                                  models.Mock.status == "published")
    ).all()
    available = sum(1 for m in published if _primary_section_key(m) == section)

    return {
        "exam": exam, "section": section,
        "attempted": len(rows), "available": available,
        "latestScore": rows[-1]["score"] if rows else 0,
        "bestScore": max(scores) if scores else 0,
        "marksTotal": rows[-1]["marksTotal"] if rows else 0,
        "avgAccuracy": round(sum(accs) / len(accs), 4) if accs else 0.0,
        "scoreTrend": [{"label": f"M{i+1}", "score": r["score"], "marksTotal": r["marksTotal"]}
                       for i, r in enumerate(rows)],
        "accuracyTrend": [{"label": f"M{i+1}", "accuracy": r["accuracy"]} for i, r in enumerate(rows)],
        "timeTrend": [{"label": f"M{i+1}", "avgTimePerQ": r["avgTimePerQ"]} for i, r in enumerate(rows)],
        "attempts": rows,
    }


def mock_summary(db, learner, exam: str) -> dict:
    """The dashboard 'Best Mock' / 'Last Mock' cards: the learner's best (highest %) and most recent
    attempt for both sectional and full mocks in one exam. Each card is null when there's no attempt."""
    exam = (exam or "").upper()
    attempts = db.scalars(
        select(models.MockAttempt).where(
            models.MockAttempt.learner_id == learner.id,
            models.MockAttempt.exam_code == exam,
        ).order_by(models.MockAttempt.created_at)  # oldest -> newest
    ).all()

    def card(a):
        if a is None:
            return None
        ov = a.overall or {}
        mt = ov.get("marks_total", 0) or 0
        score = ov.get("score", 0)
        return {
            "mockId": a.mock_id, "name": a.mock_name, "type": a.mock_type,
            "section": a.section_key, "score": score, "marksTotal": mt,
            "pct": round(100 * score / mt, 1) if mt else 0.0,
            "date": a.created_at.isoformat() if a.created_at else None,
        }

    def best(lst):
        cand = [a for a in lst if (a.overall or {}).get("marks_total")]
        if not cand:
            return None
        return max(cand, key=lambda a: a.overall.get("score", 0) / (a.overall.get("marks_total") or 1))

    sect = [a for a in attempts if a.mock_type == "sectional"]
    full = [a for a in attempts if a.mock_type == "full"]
    return {
        "exam": exam,
        "bestSectional": card(best(sect)),
        "lastSectional": card(sect[-1] if sect else None),
        "bestFull": card(best(full)),
        "lastFull": card(full[-1] if full else None),
        # distinct mocks attempted (for the "attempted / available" practice tiles)
        "sectionalAttempted": len({a.mock_id for a in sect}),
        "fullAttempted": len({a.mock_id for a in full}),
    }


def full_analysis(db, learner, exam: str) -> dict:
    """Aggregate across every FULL-mock attempt the learner has made in one exam: attempts count,
    avg/best/lowest score, average accuracy + time, score/accuracy/time trends, and per-section
    trends + averages (VARC / DILR / QA for CAT)."""
    exam = (exam or "").upper()
    attempts = db.scalars(
        select(models.MockAttempt).where(
            models.MockAttempt.learner_id == learner.id,
            models.MockAttempt.exam_code == exam,
            models.MockAttempt.mock_type == "full",
        ).order_by(models.MockAttempt.created_at)
    ).all()

    rows, section_names = [], []
    for a in attempts:
        ov = a.overall or {}
        n_q = ov.get("total", 0) or 0
        rows.append({
            "attemptId": str(a.id), "mockId": a.mock_id, "mockName": a.mock_name,
            "score": ov.get("score", 0), "marksTotal": ov.get("marks_total", 0),
            "raw": ov.get("raw", 0), "total": n_q, "attempted": ov.get("attempted", 0),
            "accuracy": ov.get("accuracy", 0.0),
            "avgTimePerQ": round((a.time_ms / 1000) / n_q, 1) if n_q else 0.0,
            "sections": a.section_scores or [],
            "completedAt": a.created_at.isoformat() if a.created_at else None,
        })
        for s in (a.section_scores or []):
            if s.get("name") and s["name"] not in section_names:
                section_names.append(s["name"])

    scores = [r["score"] for r in rows]
    accs = [r["accuracy"] for r in rows]
    times = [r["avgTimePerQ"] for r in rows]

    # per-section trends + averages
    sections = {}
    for name in section_names:
        strend, atrend = [], []
        s_scores, s_accs = [], []
        for i, r in enumerate(rows):
            sc = next((x for x in r["sections"] if x.get("name") == name), None)
            if sc:
                strend.append({"label": f"M{i+1}", "score": sc.get("score", 0),
                               "marksTotal": sc.get("marks_total", 0)})
                atrend.append({"label": f"M{i+1}", "accuracy": sc.get("accuracy", 0.0)})
                s_scores.append(sc.get("score", 0))
                s_accs.append(sc.get("accuracy", 0.0))
        sections[name] = {
            "avgScore": round(sum(s_scores) / len(s_scores), 1) if s_scores else 0,
            "bestScore": max(s_scores) if s_scores else 0,
            "avgAccuracy": round(sum(s_accs) / len(s_accs), 4) if s_accs else 0.0,
            "scoreTrend": strend, "accuracyTrend": atrend,
        }

    published = db.scalars(
        select(models.Mock).where(models.Mock.exam_code == exam, models.Mock.type == "full",
                                  models.Mock.status == "published")
    ).all()

    return {
        "exam": exam,
        "attempted": len(rows), "available": len(published),
        "avgScore": round(sum(scores) / len(scores), 1) if scores else 0,
        "bestScore": max(scores) if scores else 0,
        "lowestScore": min(scores) if scores else 0,
        "marksTotal": rows[-1]["marksTotal"] if rows else 0,
        "avgAccuracy": round(sum(accs) / len(accs), 4) if accs else 0.0,
        "avgTimePerQ": round(sum(times) / len(times), 1) if times else 0.0,
        "scoreTrend": [{"label": f"M{i+1}", "score": r["score"], "marksTotal": r["marksTotal"]}
                       for i, r in enumerate(rows)],
        "accuracyTrend": [{"label": f"M{i+1}", "accuracy": r["accuracy"]} for i, r in enumerate(rows)],
        "timeTrend": [{"label": f"M{i+1}", "avgTimePerQ": r["avgTimePerQ"]} for i, r in enumerate(rows)],
        "sections": sections,
        "attempts": rows,
    }
