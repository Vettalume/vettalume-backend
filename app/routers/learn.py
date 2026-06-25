from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models
from ..deps import get_current_learner, get_db
from ..schemas import LearnAnswerIn
from ..services import knowledge_graph as kg
from ..services import learning

router = APIRouter(prefix="/learn", tags=["learn"])


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
