"""Public "Contact us" form submissions. Stored for admins to read/triage (see /admin/contact)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models
from ..deps import get_db

router = APIRouter(prefix="/contact", tags=["contact"])


class ContactIn(BaseModel):
    firstName: str = Field("", max_length=120)
    lastName: str = Field("", max_length=120)
    phone: str = Field("", max_length=40)
    email: str = Field("", max_length=255)
    message: str = Field("", max_length=5000)


@router.post("")
def submit_contact(body: ContactIn, db: Session = Depends(get_db)) -> dict:
    message = (body.message or "").strip()
    email = (body.email or "").strip()
    phone = (body.phone or "").strip()
    if not message:
        raise HTTPException(400, "Please write a message.")
    if not email and not phone:
        raise HTTPException(400, "Please leave an email or phone so we can reach you.")
    m = models.ContactMessage(
        id=uuid.uuid4().hex,
        first_name=(body.firstName or "").strip()[:120],
        last_name=(body.lastName or "").strip()[:120],
        phone=phone[:40],
        email=email[:255],
        message=message[:5000],
    )
    db.add(m)
    db.commit()
    return {"ok": True}
