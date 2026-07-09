from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..deps import get_current_learner, get_db
from ..schemas import AnswerIn, AnswerOut, ItemPublic, NodeStateOut, StateOut
from ..services import analytics, learning
from ..services.state import eligible_items, node_attempt_count, record_response

router = APIRouter(prefix="/practice", tags=["practice"])


@router.get("/session")
def practice_session(exam: str, topic: Optional[str] = None, topic_id: Optional[str] = None,
                     limit: int = 1000, learner=Depends(get_current_learner),
                     db: Session = Depends(get_db)) -> dict:
    """A set of practice questions for one chapter, delivered like a sectional mock (a palette of
    questions to work through). Identify the chapter by `topic` (name) or `topic_id`."""
    exam = (exam or "").upper()
    if db.get(models.Exam, exam) is None:
        raise HTTPException(404, f"unknown exam '{exam}'")
    node = analytics.resolve_chapter(db, exam, topic, topic_id)
    if node is None:
        raise HTTPException(404, f"no chapter '{topic or topic_id}' in exam '{exam}'")
    return learning.practice_batch(db, learner, node, limit=limit)


@router.get("/adaptive")
def practice_adaptive(exam: str, topic: Optional[str] = None, topic_id: Optional[str] = None,
                      exclude: Optional[str] = None, learner=Depends(get_current_learner),
                      db: Session = Depends(get_db)) -> dict:
    """The MAB's next single practice question for a chapter (one at a time, no jumping). Pool is the
    chapter's practice bank only. `exclude` is a comma-separated list of item ids shown this session."""
    exam = (exam or "").upper()
    if db.get(models.Exam, exam) is None:
        raise HTTPException(404, f"unknown exam '{exam}'")
    node = analytics.resolve_chapter(db, exam, topic, topic_id)
    if node is None:
        raise HTTPException(404, f"no chapter '{topic or topic_id}' in exam '{exam}'")
    skip = frozenset(p.strip() for p in exclude.split(",") if p.strip()) if exclude else frozenset()
    return learning.practice_next(db, learner, node, exclude_item_ids=skip)


@router.get("/next", response_model=ItemPublic)
def next_item(node_id: str, learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> ItemPublic:
    """Return the next question for a concept. Phase 0 picks the first eligible item; Phase 1's
    problem bandit replaces the selection. The learner never sees difficulty, the answer, or the
    solution here."""
    if db.get(models.KnowledgeNode, node_id) is None:
        raise HTTPException(404, f"unknown node '{node_id}'")
    candidates = eligible_items(db, learner.id, context="practice", concept_node_id=node_id)
    if not candidates:
        raise HTTPException(404, "no eligible items for this concept")
    it = candidates[0]
    return ItemPublic(item_id=it.item_id, stem=it.stem, options=it.options,
                      format=it.format, num_options=it.num_options)


@router.post("/answer", response_model=AnswerOut)
def submit_answer(body: AnswerIn, learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> AnswerOut:
    item = db.get(models.Item, body.item_id)
    if item is None:
        raise HTTPException(404, f"unknown item '{body.item_id}'")

    _resp, correct, state = record_response(
        db, learner, item, context=body.context.value, answer_given=body.answer_given,
        correct=body.correct, response_time_ms=body.response_time_ms,
        attempt_number=body.attempt_number, hints_used=body.hints_used, session_id=body.session_id,
    )
    attempts = node_attempt_count(db, learner.id, item.concept_node_id)
    return AnswerOut(correct=correct, solution=item.solution, node_id=item.concept_node_id,
                     mastery=round(state.mastery, 4), attempts=attempts)


@router.get("/state", response_model=StateOut)
def get_state(exam: str, learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> StateOut:
    nodes = db.scalars(select(models.KnowledgeNode).where(models.KnowledgeNode.exam_code == exam)).all()
    states = {s.node_id: s for s in db.scalars(
        select(models.LearnerNodeState).where(models.LearnerNodeState.learner_id == learner.id)).all()}

    out: list[NodeStateOut] = []
    for n in nodes:
        st = states.get(n.id)
        attempts = node_attempt_count(db, learner.id, n.id) if st else 0
        out.append(NodeStateOut(
            node_id=n.id, name=n.name,
            learned=bool(st.learned) if st else False,
            mastery=round(st.mastery, 4) if st else 0.0,
            attempts=attempts,
        ))
    return StateOut(exam=exam, nodes=out)
