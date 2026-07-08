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


@router.get("/{mid}")
def get_mock(mid: str, learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    """The paper to take — sections + questions with the answer key stripped."""
    return student_mocks.paper(db, mid)


class MockSubmitIn(BaseModel):
    answers: dict = Field(default_factory=dict)  # { question_id : selected_option_index }


@router.post("/{mid}/submit")
def submit_mock(mid: str, body: MockSubmitIn,
                learner=Depends(get_current_learner), db: Session = Depends(get_db)) -> dict:
    """Grade a submission and return the raw score (per section + overall)."""
    return student_mocks.submit(db, learner, mid, body.answers)
