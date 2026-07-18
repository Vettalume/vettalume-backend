"""Student-facing diagnostic test endpoints (Phase 18).

Distinct from /diagnosis (the cause/leak analysis). Flow: status -> start (get the paper) ->
submit (graded, writes per-section ability, locked once-only) -> result.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..deps import get_current_learner, get_db
from ..services import billing, diagnostic

router = APIRouter(prefix="/diagnostic", tags=["diagnostic"])


class SubmitIn(BaseModel):
    answers: dict = Field(default_factory=dict)   # { question_id : selected_option_index }


@router.get("/status")
def get_status(exam: str, learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    """available | in_progress | completed | not_configured — and whether a diagnostic exists."""
    return diagnostic.status(db, learner, exam)


@router.post("/start")
def start(exam: str, learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    """Fetch the diagnostic paper (no answers). 409 if the learner has already completed it."""
    billing.guard_diagnostic(db, learner, exam)   # gated by plan (no-op unless enforcing; all tiers include it)
    return diagnostic.start(db, learner, exam)


@router.post("/submit")
def submit(exam: str, body: SubmitIn, learner=Depends(get_current_learner),
           db: Session = Depends(get_db)) -> dict:
    """Grade the attempt, write each section's ability, and lock the diagnostic. 409 if already taken."""
    return diagnostic.submit(db, learner, exam, body.answers)


@router.get("/result")
def result(exam: str, learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    """Per-section ability from the completed diagnostic."""
    return diagnostic.result(db, learner, exam)
