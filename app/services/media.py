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


def resolve(explicit_image: str, candidates, keys: set[str]) -> str:
    """An explicit image URL wins; otherwise auto-attach /media/{key} for the first candidate id that
    has an uploaded image. `candidates` may be a single id or a list — a question is matched by its
    generated id OR its author id (the Excel "Question ID", stored as externalId / external_id), so
    images named by either work."""
    if explicit_image:
        return explicit_image
    ids = candidates if isinstance(candidates, (list, tuple, set)) else [candidates]
    for cid in ids:
        if cid and str(cid) in keys:
            return f"/media/{cid}"
    return ""
