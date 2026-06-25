"""Question-bank importer — reads the Vettalume authoring template (exact column layout) and:
  1. builds the knowledge graph (topics, concepts) from the Topic / Subtopic columns,
  2. wires prerequisites from the Prerequisites column (subtopic-level),
  3. ingests the questions (A/B/C/D letter answers resolved, difficulty -2..2, passage sets, status),
  4. leaves IRT b / a / c EMPTY — those are calculated later by calibration, never authored.

Taxonomy nodes are reused by name, so re-importing the same sheet doesn't create duplicates.
Whole-batch atomic: any error rejects everything (taxonomy included).
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..schemas import ItemFormatIn, ItemIn
from .ingestion import ingest_items

# ---- template headers (exact strings from the sheet) ----
H_ID = "Question ID"
H_EXAM = "Exam"
H_SECTION = "Section"
H_TOPIC = "Topic"
H_SUB = "Subtopic"
H_PRE = "Prerequisites"
H_DIFF = "Difficulty (-2 to 2)"
H_FMT = "Question format"
H_TEXT = "Question text"
H_OPTS = ["Option A", "Option B", "Option C", "Option D", "Option E"]
H_CORRECT = "Correct answer"
H_SOL = "Solution / explanation"
H_TIME = "Expected time (sec)"
H_PASSAGE = "Passage / Set ID"
H_SOURCE = "Source"
H_STATUS = "Status"
H_TYPE = "Question type"   # rich archetype (Problem Solving, Data Sufficiency, ...); stored as archetype_id
# H_IRT_B = "IRT b" is intentionally NOT read — it is derived by calibration.

NONE_TOKENS = {"", "-", "n/a", "na", "none", "null"}
_LETTER = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}


def _clean(v):
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in NONE_TOKENS else s


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "x"


def _int_or_none(v):
    s = _clean(v)
    if s is None:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _split_prereqs(s):
    if not s:
        return []
    return [p.strip() for p in re.split(r"[;,/|]", s)
            if p.strip() and p.strip().lower() not in NONE_TOKENS]


def _resolve_answer(opt_vals, correct_raw):
    """Map the authored answer onto a stored format. Returns (format, options, correct_value).

      single letter + options  -> ('mcq', options, that option's value)
      value typed in directly   -> ('mcq', options, that value)
      multiple letters ("A, C") -> ('tita', None, joined resolved values)   # multi-select
      a sequence / number       -> ('tita', None, the literal answer)        # e.g. "SPRQ", "180"

    Format is DERIVED from the answer shape, not from the 'Question type' label — so a 5-option
    MCQ, a sequence, and a two-answer item all land in the right bucket without a per-exam rule.
    """
    opts = [o for o in opt_vals if o is not None]
    tokens = [t for t in re.split(r"[,\s/|]+", correct_raw.strip()) if t]
    letters = [t.upper() for t in tokens if t.upper() in _LETTER]
    resolved = [opt_vals[_LETTER[L]] for L in letters
                if _LETTER[L] < len(opt_vals) and opt_vals[_LETTER[L]] is not None]

    if len(opts) >= 2 and len(letters) == 1 and len(resolved) == 1:
        return "mcq", opts, resolved[0]
    if len(opts) >= 2 and len(resolved) >= 2:
        return "tita", None, ", ".join(resolved)
    if len(opts) >= 2 and correct_raw in opts:
        return "mcq", opts, correct_raw
    return "tita", None, correct_raw


def import_question_bank(db: Session, rows: list[dict], usage_scope: str | None = None) -> dict:
    errors: list[dict] = []
    norm: list[dict] = []
    seen_ids: set[str] = set()

    # ---- normalise + validate each row ----
    for i, r in enumerate(rows):
        ln = i + 2  # +1 header, +1 to 1-index
        rid = _clean(r.get(H_ID))
        if not rid:
            errors.append({"row": ln, "error": "missing Question ID"})
            continue
        if rid in seen_ids:
            errors.append({"row": ln, "item_id": rid, "error": "duplicate Question ID in file"})
            continue
        seen_ids.add(rid)

        exam, section = _clean(r.get(H_EXAM)), _clean(r.get(H_SECTION))
        topic, sub = _clean(r.get(H_TOPIC)), _clean(r.get(H_SUB))
        if not all([exam, section, topic, sub]):
            errors.append({"row": ln, "item_id": rid, "error": "Exam/Section/Topic/Subtopic all required"})
            continue

        try:
            diff = int(float(str(r.get(H_DIFF)).strip()))
        except (TypeError, ValueError):
            errors.append({"row": ln, "item_id": rid, "error": "Difficulty must be an integer -2..2"})
            continue
        if not -2 <= diff <= 2:
            errors.append({"row": ln, "item_id": rid, "error": f"Difficulty {diff} out of range -2..2"})
            continue

        stem = _clean(r.get(H_TEXT))
        if not stem:
            errors.append({"row": ln, "item_id": rid, "error": "missing Question text"})
            continue

        opt_vals = [_clean(r.get(h)) for h in H_OPTS]
        correct_raw = _clean(r.get(H_CORRECT))
        if not correct_raw:
            errors.append({"row": ln, "item_id": rid, "error": "missing Correct answer"})
            continue
        fmt, opts, correct = _resolve_answer(opt_vals, correct_raw)

        norm.append({
            "id": rid, "exam": exam, "section": section, "topic": topic, "sub": sub,
            "archetype": _clean(r.get(H_TYPE)),
            "prereqs": _split_prereqs(_clean(r.get(H_PRE))),
            "diff": diff, "fmt": fmt, "stem": stem, "opts": opts, "correct": correct,
            "solution": _clean(r.get(H_SOL)), "time": _int_or_none(r.get(H_TIME)),
            "passage": _clean(r.get(H_PASSAGE)), "source": _clean(r.get(H_SOURCE)),
            "status": (_clean(r.get(H_STATUS)) or "approved").lower(),
        })

    if errors:
        return {"status": "rejected", "received": len(rows), "errors": errors}

    # ---- Pass 1: build taxonomy (reuse by name) ----
    topics_created = concepts_created = 0
    concept_id_by_name: dict[tuple, str] = {}
    for n in norm:
        _ensure_exam(db, n["exam"])
        sec = _ensure_section(db, n["exam"], n["section"])
        topic_node, t_new = _ensure_topic(db, n["exam"], sec, n["topic"])
        topics_created += t_new
        concept_node, c_new = _ensure_concept(db, n["exam"], sec, topic_node, n["sub"])
        concepts_created += c_new
        n["concept_id"], n["sec_obj"] = concept_node.id, sec
        concept_id_by_name[(n["exam"], n["section"], n["sub"].lower())] = concept_node.id
    db.flush()

    # ---- Pass 2: prerequisites (dangling refs are warnings, not rejections) ----
    warnings: list[dict] = []
    edges_created = 0
    added_edges: set[tuple[str, str]] = set()   # in-batch dedupe (session autoflush is off)
    for n in norm:
        for pre_name in n["prereqs"]:
            pre_id = concept_id_by_name.get((n["exam"], n["section"], pre_name.lower())) \
                or _find_concept_id(db, n["exam"], n["sec_obj"], pre_name)
            if pre_id is None:
                warnings.append({"item_id": n["id"],
                                 "warning": f"prerequisite '{pre_name}' not found among "
                                            f"{n['exam']}/{n['section']} subtopics — edge skipped"})
                continue
            key = (n["concept_id"], pre_id)
            if pre_id == n["concept_id"] or key in added_edges:
                continue
            if _edge_exists(db, n["concept_id"], pre_id):
                continue
            db.add(models.PrereqEdge(node_id=n["concept_id"], prereq_node_id=pre_id))
            added_edges.add(key)
            edges_created += 1
    db.flush()

    # ---- Pass 3: questions (reuse the QC gate; commits the whole transaction on success) ----
    from ..schemas import UsageScopeIn
    _scope = UsageScopeIn(usage_scope) if usage_scope else UsageScopeIn.both
    items = [ItemIn(
        item_id=n["id"], exam_code=n["exam"], section_key=n["section"], concept_node_id=n["concept_id"],
        archetype_id=n["archetype"], difficulty_d=n["diff"], format=ItemFormatIn(n["fmt"]),
        num_options=(len(n["opts"]) if n["opts"] else 0), stem=n["stem"], options=n["opts"],
        correct_answer=n["correct"], solution=n["solution"], time_benchmark_s=n["time"],
        passage_set_id=n["passage"], status=n["status"], usage_scope=_scope,
        provenance=({"source": n["source"]} if n["source"] else None),
    ) for n in norm]
    report = ingest_items(db, items)

    return {
        "status": report.status, "received": len(rows),
        "knowledge_graph": {"topics_created": topics_created, "concepts_created": concepts_created,
                            "prereq_edges_created": edges_created},
        "questions": {"inserted": report.inserted, "updated": report.updated,
                      "unchanged": report.unchanged},
        "warnings": warnings,
        "errors": [e.model_dump() for e in report.errors],
    }


# ---- taxonomy helpers (reuse-by-name) ----
def _ensure_exam(db, code):
    e = db.get(models.Exam, code)
    if e is None:
        e = models.Exam(code=code, name=code)
        db.add(e)
        db.flush()
    return e


def _ensure_section(db, exam, key):
    sec = db.scalar(select(models.Section).where(
        models.Section.exam_code == exam, models.Section.key == key))
    if sec is None:
        sec = models.Section(exam_code=exam, key=key, name=key)
        db.add(sec)
        db.flush()
    return sec


def _ensure_topic(db, exam, sec, name):
    t = db.scalar(select(models.KnowledgeNode).where(
        models.KnowledgeNode.exam_code == exam, models.KnowledgeNode.section_id == sec.id,
        models.KnowledgeNode.kind == models.NodeKind.topic.value, models.KnowledgeNode.name == name))
    if t:
        return t, 0
    t = models.KnowledgeNode(id=_unique_id(db, f"{exam.lower()}-{_slug(sec.key)}-{_slug(name)}"),
                             exam_code=exam, section_id=sec.id, kind=models.NodeKind.topic.value, name=name)
    db.add(t)
    db.flush()
    return t, 1


def _ensure_concept(db, exam, sec, topic, name):
    c = db.scalar(select(models.KnowledgeNode).where(
        models.KnowledgeNode.parent_id == topic.id,
        models.KnowledgeNode.kind == models.NodeKind.concept.value, models.KnowledgeNode.name == name))
    if c:
        return c, 0
    c = models.KnowledgeNode(id=_unique_id(db, f"{exam.lower()}-{_slug(sec.key)}-{_slug(name)}"),
                             exam_code=exam, section_id=sec.id, kind=models.NodeKind.concept.value,
                             name=name, parent_id=topic.id)
    db.add(c)
    db.flush()
    return c, 1


def _unique_id(db, base):
    nid, n = base, 2
    while db.get(models.KnowledgeNode, nid) is not None:
        nid = f"{base}-{n}"
        n += 1
    return nid


def _find_concept_id(db, exam, sec, name):
    c = db.scalar(select(models.KnowledgeNode).where(
        models.KnowledgeNode.exam_code == exam, models.KnowledgeNode.section_id == sec.id,
        models.KnowledgeNode.kind == models.NodeKind.concept.value, models.KnowledgeNode.name == name))
    return c.id if c else None


def _edge_exists(db, node_id, prereq_id):
    return db.scalar(select(models.PrereqEdge).where(
        models.PrereqEdge.node_id == node_id,
        models.PrereqEdge.prereq_node_id == prereq_id)) is not None
