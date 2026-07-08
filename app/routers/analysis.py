from __future__ import annotations

import random
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..deps import get_current_learner, get_db
from ..services import analytics, learning

router = APIRouter(prefix="/analysis", tags=["analysis"])


@router.get("/chapter")
def chapter(exam: str, topic: Optional[str] = None, topic_id: Optional[str] = None,
            learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    """Full per-chapter analytics for the current learner: KPIs, difficulty spread, improvement
    over time, learning-vs-practice split, strongest/weakest concepts, MAB-recommended actions,
    and the subtopic breakdown. Identify the chapter by `topic` (name) or `topic_id`."""
    exam = (exam or "").upper()
    if db.get(models.Exam, exam) is None:
        raise HTTPException(404, f"unknown exam '{exam}'")
    node = analytics.resolve_chapter(db, exam, topic, topic_id)
    if node is None:
        raise HTTPException(404, f"no chapter '{topic or topic_id}' in exam '{exam}'")
    return analytics.chapter_analysis(db, learner, node)


@router.get("/chapter/attempts")
def chapter_attempts(exam: str, topic: Optional[str] = None, topic_id: Optional[str] = None,
                     learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    """The questions the learner has solved in this chapter (subtopic quizzes + topic practice),
    with their answer, correctness, the correct answer, and the solution — powers 'revisit'."""
    exam = (exam or "").upper()
    if db.get(models.Exam, exam) is None:
        raise HTTPException(404, f"unknown exam '{exam}'")
    node = analytics.resolve_chapter(db, exam, topic, topic_id)
    if node is None:
        raise HTTPException(404, f"no chapter '{topic or topic_id}' in exam '{exam}'")
    return analytics.chapter_attempts(db, learner, node)


@router.post("/simulate")
def simulate(exam: str, steps: int = 40, accuracy: float = 0.8, seed: int = 7,
             learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    """DEV ONLY — drive the MAB for the current learner and record real responses, so the analytics
    have something to show without hand-answering. Reads each served item's correct answer from the
    DB and answers correctly with probability `accuracy`."""
    if not settings.dev_mode:
        raise HTTPException(403, "simulate is only available in dev_mode")
    if db.get(models.Exam, exam) is None:
        raise HTTPException(404, f"unknown exam '{exam}'")

    rng = random.Random(seed)
    answered = correct = 0
    last_item: str | None = None
    for _ in range(max(1, steps)):
        # exclude the just-served item so the drive spreads across concepts (breadth + some revise),
        # instead of hammering one weak concept when answers are wrong
        excl = frozenset([last_item]) if last_item else frozenset()
        nxt = learning.next_step(db, learner, exam, exclude_item_ids=excl)
        if nxt.get("status") != "ok":
            nxt = learning.next_step(db, learner, exam)  # nothing left to exclude — try unrestricted
            if nxt.get("status") != "ok":
                break
        item = db.get(models.Item, nxt["question"]["item_id"])
        last_item = item.item_id
        given = item.correct_answer if rng.random() < accuracy else "__simulated_wrong__"
        out = learning.answer(db, learner, item, answer_given=given,
                              response_time_ms=rng.randint(20_000, 150_000), session_id="sim")
        answered += 1
        correct += 1 if out["correct"] else 0
    return {"exam": exam, "answered": answered, "correct": correct,
            "accuracy": round(correct / answered, 4) if answered else 0.0,
            "note": "responses recorded for the current learner — open /chapter to see the analysis"}
