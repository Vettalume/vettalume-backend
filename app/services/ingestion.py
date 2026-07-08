"""Bulk item ingestion — the QC gate.

Contract enforced here:
  * Validate the WHOLE batch first; on any error, reject everything (no partial imports).
  * Tags must reference existing exam / section / concept node (controlled vocabulary, not free text).
  * Idempotent: re-uploading identical content is a no-op (dedup on content hash).
  * Content change -> new version, and calibration fields are wiped (changed content must recalibrate).
  * Only authored fields are written. (schemas.ItemIn already forbids authoring IRT params.)
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..schemas import BENCH, IngestError, IngestReport, ItemIn


def _content_hash(item: ItemIn) -> str:
    payload = item.model_dump(mode="json")
    payload.pop("provenance", None)  # provenance is metadata, not content identity
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def ingest_items(db: Session, rows: list[ItemIn]) -> IngestReport:
    errors: list[IngestError] = []
    resolved: list[tuple[ItemIn, models.Section]] = []

    # cache the controlled vocabulary
    exam_codes = {e.code for e in db.scalars(select(models.Exam)).all()}
    sections = {(s.exam_code, s.key): s for s in db.scalars(select(models.Section)).all()}
    node_ids = {n.id for n in db.scalars(select(models.KnowledgeNode)).all()}

    seen_ids: set[str] = set()
    for i, row in enumerate(rows):
        if row.item_id in seen_ids:
            errors.append(IngestError(index=i, item_id=row.item_id, error="duplicate item_id within batch"))
            continue
        seen_ids.add(row.item_id)

        if row.exam_code not in exam_codes:
            errors.append(IngestError(index=i, item_id=row.item_id, error=f"unknown exam_code '{row.exam_code}'"))
            continue
        sec = sections.get((row.exam_code, row.section_key))
        if sec is None:
            errors.append(IngestError(index=i, item_id=row.item_id,
                                      error=f"unknown section '{row.section_key}' for exam '{row.exam_code}'"))
            continue
        if row.concept_node_id not in node_ids:
            errors.append(IngestError(index=i, item_id=row.item_id,
                                      error=f"unknown concept_node_id '{row.concept_node_id}'"))
            continue
        if row.format == "mcq" and (not row.options or len(row.options) < 2):
            errors.append(IngestError(index=i, item_id=row.item_id, error="mcq requires >= 2 options"))
            continue
        if row.format == "mcq" and row.correct_answer not in (row.options or []):
            errors.append(IngestError(index=i, item_id=row.item_id, error="correct_answer must be one of the options"))
            continue
        resolved.append((row, sec))

    if errors:
        return IngestReport(status="rejected", received=len(rows), errors=errors)

    inserted = updated = unchanged = 0
    # Pre-load existing items in one query (chunked) instead of db.get() per row — that per-row
    # lookup was an N+1 that cost ~one network round trip per item over a remote DB (Neon).
    existing_by_id: dict = {}
    batch_ids = [row.item_id for row, _ in resolved]
    for j in range(0, len(batch_ids), 1000):
        chunk = batch_ids[j:j + 1000]
        for it in db.scalars(select(models.Item).where(models.Item.item_id.in_(chunk))).all():
            existing_by_id[it.item_id] = it

    for row, sec in resolved:
        h = _content_hash(row)
        tb = row.time_benchmark_s if row.time_benchmark_s is not None else BENCH.get(row.difficulty_d)
        existing = existing_by_id.get(row.item_id)

        if existing is None:
            db.add(models.Item(
                item_id=row.item_id, version=1, content_hash=h,
                exam_code=row.exam_code, section_id=sec.id, concept_node_id=row.concept_node_id,
                archetype_id=row.archetype_id, grid_cell=row.grid_cell,
                difficulty_d=row.difficulty_d, format=row.format.value, num_options=row.num_options,
                negative_marking=row.negative_marking, stem=row.stem, options=row.options,
                correct_answer=row.correct_answer, distractor_map=row.distractor_map,
                solution=row.solution, time_benchmark_s=tb, provenance=row.provenance,
                usage_scope=row.usage_scope.value,
                passage_set_id=row.passage_set_id, status=row.status,
            ))
            inserted += 1
        elif existing.content_hash == h:
            existing.provenance = row.provenance or existing.provenance  # idempotent re-upload
            unchanged += 1
        else:
            # content changed -> bump version and invalidate calibration
            existing.version += 1
            existing.content_hash = h
            existing.exam_code = row.exam_code
            existing.section_id = sec.id
            existing.concept_node_id = row.concept_node_id
            existing.archetype_id = row.archetype_id
            existing.grid_cell = row.grid_cell
            existing.difficulty_d = row.difficulty_d
            existing.format = row.format.value
            existing.num_options = row.num_options
            existing.negative_marking = row.negative_marking
            existing.stem = row.stem
            existing.options = row.options
            existing.correct_answer = row.correct_answer
            existing.distractor_map = row.distractor_map
            existing.solution = row.solution
            existing.time_benchmark_s = tb
            existing.provenance = row.provenance
            existing.usage_scope = row.usage_scope.value
            existing.passage_set_id = row.passage_set_id
            existing.status = row.status
            existing.irt_a = existing.irt_b = existing.irt_c = None
            existing.empirical = None
            existing.calibration_run_id = None
            existing.calibrated_at = None
            updated += 1

    db.commit()
    return IngestReport(status="committed", received=len(rows),
                        inserted=inserted, updated=updated, unchanged=unchanged)
