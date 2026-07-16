"""Question-image helpers, shared by the media router, the admin upload endpoint, and the question
serializers (mocks / diagnostic / practice). Images are keyed by the question/item id."""
from __future__ import annotations

import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models


def key_from_filename(filename: str) -> str:
    """`sets/q123.PNG` -> `q123` — the question/item id the image belongs to."""
    return os.path.splitext(os.path.basename(filename or ""))[0].strip()


def existing_keys(db: Session, ids) -> set[str]:
    """Which of these question ids have an uploaded image (batched, one query)."""
    wanted = [str(i) for i in ids if i]
    if not wanted:
        return set()
    return set(db.scalars(select(models.MediaAsset.key).where(models.MediaAsset.key.in_(wanted))).all())


def resolve(explicit_image: str, qid, keys: set[str]) -> str:
    """An explicit image URL wins; otherwise auto-attach /media/{qid} when one was uploaded."""
    if explicit_image:
        return explicit_image
    return f"/media/{qid}" if str(qid) in keys else ""
