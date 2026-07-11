"""Student-facing catalog + taking flow for admin-authored mocks (sectional / full).

Prefix `/mocks` (plural) — distinct from `/mock` (singular), which is the adaptive IRT engine.
An admin publishes a fixed-form Mock in the content portal; these endpoints let a student list the
published ones, fetch a paper to take (no answer key), and submit it for a raw score. Detailed
post-mock analysis is a later phase.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models
from ..deps import get_current_learner, get_db
from ..services import student_mocks

router = APIRouter(prefix="/mocks", tags=["mocks"])


@router.get("")
def list_mocks(exam: str, type: Optional[str] = None,
               learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    """Published sectional/full mocks for an exam. `type` optionally filters to 'sectional' or 'full'.
    Empty (count 0) until an admin publishes one — the mock lists start at zero."""
    exam = (exam or "").upper()
    if db.get(models.Exam, exam) is None:
        raise HTTPException(404, f"unknown exam '{exam}'")
    return student_mocks.list_published(db, exam, type)


# NOTE: these specific routes are declared BEFORE "/{mid}" so they aren't captured by it.
@router.get("/attempts/{attempt_id}")
def attempt_analysis(attempt_id: str,
                     learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    """Full analysis of ONE completed mock attempt (per-question review + section/overall scores)."""
    return student_mocks.individual_analysis(db, learner, attempt_id)


@router.get("/section-analysis")
def section_analysis(exam: str, section: str,
                     learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    """Aggregate analytics across every sectional-mock attempt the learner has made in one section."""
    exam = (exam or "").upper()
    if db.get(models.Exam, exam) is None:
        raise HTTPException(404, f"unknown exam '{exam}'")
    return student_mocks.section_analysis(db, learner, exam, section)


@router.get("/full-analysis")
def full_analysis(exam: str,
                  learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    """Aggregate analytics across every FULL-mock attempt the learner has made in one exam."""
    exam = (exam or "").upper()
    if db.get(models.Exam, exam) is None:
        raise HTTPException(404, f"unknown exam '{exam}'")
    return student_mocks.full_analysis(db, learner, exam)


@router.get("/{mid}")
def get_mock(mid: str, learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    """The paper to take — sections + questions with the answer key stripped."""
    return student_mocks.paper(db, mid)


class MockSubmitIn(BaseModel):
    answers: dict = Field(default_factory=dict)      # { question_id : selected_option_index | value }
    durations: dict = Field(default_factory=dict)    # { question_id : ms spent } (optional)
    timeMs: int = 0                                  # total time taken (optional)


@router.post("/{mid}/submit")
def submit_mock(mid: str, body: MockSubmitIn,
                learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    """Grade a submission, persist it as an attempt, and return the score + attempt id."""
    return student_mocks.submit(db, learner, mid, body.answers, body.durations, body.timeMs)
