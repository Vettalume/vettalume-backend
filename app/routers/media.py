"""Public media (question images).

Images must load inside an <img> tag, which can't send an Authorization header, so serving is public
(the same trust model as the previous external-image-URL approach). Assets are keyed by the question
/item id, so a question with no explicit image URL auto-resolves to /media/{id} once a matching image
has been uploaded. Pure helpers live in services/media.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from .. import models
from ..deps import get_db

router = APIRouter(prefix="/media", tags=["media"])


@router.get("/{key}")
def get_media(key: str, db: Session = Depends(get_db)):
    m = db.get(models.MediaAsset, key)
    if m is None:
        raise HTTPException(404, "no such image")
    return Response(
        content=m.data,
        media_type=m.content_type or "image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )
