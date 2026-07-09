from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..deps import get_current_learner, get_db
from ..schemas import LearnAnswerIn
from ..services import knowledge_graph as kg
from ..services import learning

router = APIRouter(prefix="/learn", tags=["learn"])


@router.get("/overview")
def learn_overview(exam: str, learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    """Everything the student learning UI needs for one exam, in one call:
    sections -> chapters (topic nodes) -> subtopics (concept nodes), plus this student's progress.

    A brand-new student has no LearnerNodeState rows, so every metric here is 0 — which is exactly
    what the UI should show until they start learning. As admins add topics/concepts (and students
    make progress), this fills in automatically."""
    exam = (exam or "").upper()
    if db.get(models.Exam, exam) is None:
        raise HTTPException(404, f"unknown exam '{exam}'")

    sections = db.scalars(select(models.Section).where(models.Section.exam_code == exam)).all()
    nodes = db.scalars(select(models.KnowledgeNode).where(models.KnowledgeNode.exam_code == exam)).all()
    states = {
        s.node_id: s
        for s in db.scalars(
            select(models.LearnerNodeState).where(models.LearnerNodeState.learner_id == learner.id)
        ).all()
    }

    # items (for quiz signals) + this learner's responses, batched
    item_rows = db.execute(
        select(models.Item.item_id, models.Item.concept_node_id, models.Item.difficulty_d)
        .where(models.Item.exam_code == exam)
    ).all()
    items_by_concept: dict[str, list[str]] = {}
    diff_by_item: dict[str, int] = {}
    for iid, cnid, d in item_rows:
        items_by_concept.setdefault(cnid, []).append(iid)
        diff_by_item[iid] = d if d is not None else 0

    resp_rows = db.execute(
        select(models.Response.item_id, models.Response.correct)
        .where(models.Response.learner_id == learner.id, models.Response.exam_code == exam)
    ).all()
    answered_items: set[str] = set()
    correct_items: set[str] = set()
    for iid, ok in resp_rows:
        answered_items.add(iid)
        if ok:
            correct_items.add(iid)

    # subtopic progress weighting (chosen): read concept 25% + watch video 25% + quiz accuracy 50%
    W_READ, W_WATCH, W_QUIZ = 0.25, 0.25, 0.50

    def concept_pct(cid: str) -> int:
        st = states.get(cid)
        eng = (st.engagement or {}) if st else {}
        read = 1.0 if eng.get("read") else 0.0
        watched = 1.0 if eng.get("watched") else 0.0
        its = items_by_concept.get(cid, [])
        quiz = (sum(1 for i in its if i in correct_items) / len(its)) if its else 0.0
        return round(100 * (W_READ * read + W_WATCH * watched + W_QUIZ * quiz))

    def chapter_difficulty(concept_ids: list[str]) -> list[dict]:
        # difficulty -2..2 -> D1..D5; bar fill = accuracy (correct / answered) in that band
        bands = {b: {"answered": 0, "correct": 0, "total": 0} for b in range(1, 6)}
        for cid in concept_ids:
            for iid in items_by_concept.get(cid, []):
                b = max(1, min(5, diff_by_item.get(iid, 0) + 3))
                bands[b]["total"] += 1
                if iid in answered_items:
                    bands[b]["answered"] += 1
                if iid in correct_items:
                    bands[b]["correct"] += 1
        return [{
            "band": f"D{b}",
            "accuracy": round(100 * bands[b]["correct"] / bands[b]["answered"]) if bands[b]["answered"] else 0,
            "answered": bands[b]["answered"], "total": bands[b]["total"],
        } for b in range(1, 6)]

    def avg_pct(values: list[int]) -> int:
        return round(sum(values) / len(values)) if values else 0

    children: dict[str, list] = {}
    for n in nodes:
        if n.parent_id:
            children.setdefault(n.parent_id, []).append(n)
    topic_ids = {n.id for n in nodes if n.kind == models.NodeKind.topic.value}

    out_sections = []
    for s in sections:
        chapter_defs: list[tuple[str, str, list]] = []
        for t in [n for n in nodes if n.kind == models.NodeKind.topic.value and n.section_id == s.id]:
            concepts = [c for c in children.get(t.id, [])
                        if c.kind == models.NodeKind.concept.value and not kg.is_practice_bank(c)]
            chapter_defs.append((t.id, t.name, concepts))
        orphans = [n for n in nodes if n.kind == models.NodeKind.concept.value and not kg.is_practice_bank(n)
                   and n.section_id == s.id and (n.parent_id is None or n.parent_id not in topic_ids)]
        if orphans:
            chapter_defs.append(("_general_" + s.key, s.name, orphans))

        chapters = []
        section_pcts: list[int] = []
        for cid, cname, concepts in chapter_defs:
            subs = [{"id": c.id, "name": c.name, "pct": concept_pct(c.id)} for c in concepts]
            section_pcts.extend(x["pct"] for x in subs)
            chapters.append({
                "id": cid, "name": cname,
                "pct": avg_pct([x["pct"] for x in subs]),
                "difficulty": chapter_difficulty([c.id for c in concepts]),
                "subtopics": subs,
            })

        started = sum(1 for p in section_pcts if p > 0)
        out_sections.append({
            "key": s.key, "name": s.name,
            "syllabus": round(100 * started / len(section_pcts)) if section_pcts else 0,
            "ability": avg_pct(section_pcts),
            "mastery": avg_pct(section_pcts),
            "chapters": chapters,
        })

    return {"exam": exam, "sections": out_sections}


@router.get("/next")
def next_step(exam: str, section: Optional[str] = None, exclude: Optional[str] = None,
              learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    """The engine's decision: which topic (ZPD + bandit) -> learn/revise -> which question.
    Returns teaching content when starting a new concept. `exclude` is a comma-separated list of
    item IDs to skip (questions already shown or skipped this session)."""
    if db.get(models.Exam, exam) is None:
        raise HTTPException(404, f"unknown exam '{exam}'")
    skip = frozenset(p.strip() for p in exclude.split(",") if p.strip()) if exclude else frozenset()
    return learning.next_step(db, learner, exam, section, exclude_item_ids=skip)


@router.post("/answer")
def submit_answer(body: LearnAnswerIn,
                  learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    """Record a Learning answer (practice context): updates blended mastery, MAPLE edge, and
    review scheduling. Returns the full P/D/M breakdown."""
    item = db.get(models.Item, body.item_id)
    if item is None:
        raise HTTPException(404, f"unknown item '{body.item_id}'")
    return learning.answer(db, learner, item, answer_given=body.answer_given,
                           response_time_ms=body.response_time_ms, session_id=body.session_id)


@router.get("/map")
def learning_map(exam: str, section: Optional[str] = None,
                 learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    """The full learning map: topics with lock/mastery/recommended flags and per-concept state.
    Powers the section view (topic cards, ZPD-next badges, progress meters)."""
    if db.get(models.Exam, exam) is None:
        raise HTTPException(404, f"unknown exam '{exam}'")
    return learning.learning_map(db, learner, exam, section)


@router.get("/reviews")
def reviews(exam: str, learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    """Mastered concepts whose memory trace has decayed below threshold — due for spaced review."""
    return learning.due_reviews(db, learner, exam)


@router.get("/concept/{node_id}")
def concept_detail(node_id: str,
                   learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    """Per-concept analytics: mastery breakdown, MAPLE edge, learning progress, attempt count."""
    node = db.get(models.KnowledgeNode, node_id)
    if node is None or node.kind != models.NodeKind.concept.value:
        raise HTTPException(404, f"unknown concept '{node_id}'")
    cs = kg.concept_state(db, learner.id, node)
    content = node.theory or {}
    return {
        "concept_id": cs.node_id, "name": cs.name, "mastery": round(cs.mastery, 4),
        "breakdown": {"P": round(cs.p, 4), "D": round(cs.d, 4), "M": round(cs.m, 4)},
        "edge": round(cs.edge, 2), "learning_progress": round(cs.learning_progress, 4),
        "attempts": cs.attempts, "learned": cs.learned, "mastered": cs.mastered,
        "due_for_review": cs.due_for_review,
        "content": {"body": content.get("body", ""), "videos": content.get("videos", [])},
    }


@router.get("/concept/{node_id}/quiz")
def concept_quiz(node_id: str, learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    """Practice quiz questions for one concept (subtopic). Returns the approved MCQs an admin authored.
    Includes the correct answer + solution so the learning-page quiz can reveal feedback after each try."""
    node = db.get(models.KnowledgeNode, node_id)
    if node is None or node.kind != models.NodeKind.concept.value:
        raise HTTPException(404, f"unknown concept '{node_id}'")
    # stable order so "resume" is consistent across visits
    items = db.scalars(
        select(models.Item).where(
            models.Item.concept_node_id == node_id,
            models.Item.status == "approved",
            models.Item.usage_scope != models.UsageScope.mock_only.value,
        ).order_by(models.Item.created_at, models.Item.item_id)
    ).all()
    answered = set(
        db.scalars(
            select(models.Response.item_id).where(
                models.Response.learner_id == learner.id,
                models.Response.item_id.in_([it.item_id for it in items] or [""]),
            )
        ).all()
    )
    questions = [
        {
            "id": it.item_id,
            "format": it.format,                 # "mcq" | "tita" (numerical, type-in-the-answer)
            "difficulty": it.difficulty_d,
            "stem": it.stem,
            "options": it.options or [],
            "correct_answer": it.correct_answer,
            "solution": it.solution or "",
            "answered": it.item_id in answered,
        }
        for it in items
    ]
    # resume: index of the first not-yet-answered question (len == all done)
    next_index = next((i for i, q in enumerate(questions) if not q["answered"]), len(questions))
    return {"concept_id": node_id, "name": node.name, "next_index": next_index, "questions": questions}


class EngageIn(BaseModel):
    read: Optional[bool] = None
    watched: Optional[bool] = None


@router.post("/concept/{node_id}/engage")
def concept_engage(node_id: str, body: EngageIn,
                   learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    """Record learning-page engagement (read the concept / watched the video). Feeds the subtopic %."""
    node = db.get(models.KnowledgeNode, node_id)
    if node is None or node.kind != models.NodeKind.concept.value:
        raise HTTPException(404, f"unknown concept '{node_id}'")
    st = db.scalar(
        select(models.LearnerNodeState).where(
            models.LearnerNodeState.learner_id == learner.id,
            models.LearnerNodeState.node_id == node_id,
        )
    )
    if st is None:
        st = models.LearnerNodeState(learner_id=learner.id, node_id=node_id)
        db.add(st)
        db.flush()
    eng = dict(st.engagement or {})
    if body.read:
        eng["read"] = True
    if body.watched:
        eng["watched"] = True
    st.engagement = eng
    db.commit()
    return {"ok": True, "engagement": eng}


@router.get("/materials/{mid}/download")
def download_material(mid: str, learner=Depends(get_current_learner), db: Session = Depends(get_db)):
    """Stream a study material's file, gated by the learner's entitlement for the owning exam.
    The entitlement check is the materials paywall; it is enforced only when enforce_entitlements
    is on (so the open demo keeps working). This is where an S3 signed-URL redirect would slot in."""
    m = db.get(models.Material, mid)
    if m is None:
        raise HTTPException(404, "no such material")
    node = db.get(models.KnowledgeNode, m.node_id)
    exam = node.exam_code if node else None
    if settings.enforce_entitlements and exam:
        ent = db.scalar(select(models.Entitlement).where(
            models.Entitlement.account_id == learner.id,
            models.Entitlement.exam_code == exam,
            models.Entitlement.status.in_(("free", "active"))))
        if ent is None:
            raise HTTPException(403, f"you need access to {exam} to download this material")
    return Response(content=m.data, media_type=m.content_type,
                    headers={"Content-Disposition": f'inline; filename="{m.filename}"'})
