"""Knowledge-graph + state service.

Everything is DERIVED FROM THE SPINE (the Response table) — LearnerNodeState is just a cache.
Recomputing per-concept state from the ordered response log keeps a single source of truth and
avoids state-sync bugs.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from . import engine
from .engine import Attempt


# A chapter's "practice bank" is a hidden concept node (id ends with this suffix) holding chapter-
# level practice questions. It IS served in the practice section and counts toward the chapter's
# mastery/accuracy, but it is NOT a real subtopic — so it's hidden from the student's subtopic lists
# and skipped by the guided learn-next flow (practice-only).
PRACTICE_BANK_SUFFIX = "__practice"


def practice_bank_node_id(topic_id: str) -> str:
    return f"{topic_id}{PRACTICE_BANK_SUFFIX}"


def is_practice_bank(node) -> bool:
    nid = node if isinstance(node, str) else getattr(node, "id", "")
    return bool(nid) and nid.endswith(PRACTICE_BANK_SUFFIX)


@dataclass
class ConceptState:
    node_id: str
    name: str
    mastery: float
    p: float
    d: float
    m: float
    edge: float
    learning_progress: float
    attempts: int
    learned: bool        # has been practised at least once (taught-first -> first attempt = "learned")
    mastered: bool       # mastery >= H
    due_for_review: bool


@dataclass
class TopicView:
    node_id: str
    name: str
    section_key: str
    locked: bool
    mastery: float
    prereqs: list[str]
    concepts: list[ConceptState]


# ---------- raw attempts from the spine ----------
def concept_attempts(db: Session, learner_id: uuid.UUID, node_id: str) -> list[Attempt]:
    # Learning mastery is computed from PRACTICE responses only — mock/diagnostic (cold) responses
    # feed the IRT estimator, never the 0..1 mastery, keeping the two estimators strictly separate.
    rows = db.scalars(
        select(models.Response)
        .join(models.Item, models.Item.item_id == models.Response.item_id)
        .where(models.Response.learner_id == learner_id, models.Item.concept_node_id == node_id,
               models.Response.context == models.Context.practice.value)
        .order_by(models.Response.created_at.asc())
    ).all()
    return [Attempt(correct=1 if r.correct else 0, difficulty=r.difficulty_d,
                    ts=engine.as_utc(r.created_at)) for r in rows]


def concept_attempts_batch(db: Session, learner_id: uuid.UUID,
                           node_ids) -> dict[str, list[Attempt]]:
    """concept_attempts for MANY concepts in ONE query (avoids the per-concept N+1 in the loops).
    Returns {node_id -> attempts in created_at-ascending order}, identical to concept_attempts."""
    ids = [i for i in node_ids]
    if not ids:
        return {}
    rows = db.execute(
        select(models.Item.concept_node_id, models.Response.correct,
               models.Response.difficulty_d, models.Response.created_at)
        .join(models.Item, models.Item.item_id == models.Response.item_id)
        .where(models.Response.learner_id == learner_id,
               models.Item.concept_node_id.in_(ids),
               models.Response.context == models.Context.practice.value)
        .order_by(models.Response.created_at.asc())
    ).all()
    out: dict[str, list[Attempt]] = {}
    for cnid, correct, diff, ts in rows:
        out.setdefault(cnid, []).append(
            Attempt(correct=1 if correct else 0, difficulty=diff, ts=engine.as_utc(ts)))
    return out


def concept_item_counts_batch(db: Session, node_ids) -> dict[str, int]:
    """Servable-item count per concept in ONE query (respects the approved-only rule)."""
    ids = [i for i in node_ids]
    if not ids:
        return {}
    q = _approved_clause(select(models.Item.concept_node_id, func.count())
                         .where(models.Item.concept_node_id.in_(ids)))
    return dict(db.execute(q.group_by(models.Item.concept_node_id)).all())


def concepts_with_items(db: Session, node_ids) -> set[str]:
    """The subset of concepts that have >=1 servable item — batched replacement for calling
    _concept_has_items() once per concept."""
    ids = [i for i in node_ids]
    if not ids:
        return set()
    q = _approved_clause(select(models.Item.concept_node_id)
                         .where(models.Item.concept_node_id.in_(ids))).distinct()
    return set(db.scalars(q).all())


def _build_concept_state(node: models.KnowledgeNode, attempts: list[Attempt],
                         n_items: int, now: datetime) -> ConceptState:
    """Pure state computation from already-fetched attempts + item count (no DB access), so it can
    be driven by either the per-concept path or the batched path with identical results."""
    mastery, p, d, m = engine.blended_mastery(attempts, now)
    edge = engine.maple_edge(attempts)
    # require evidence across up to MASTERY_MIN_ATTEMPTS items, but never more than the concept has
    eff_min = max(1, min(engine.MASTERY_MIN_ATTEMPTS, n_items)) if n_items else engine.MASTERY_MIN_ATTEMPTS
    return ConceptState(
        node_id=node.id, name=node.name, mastery=mastery, p=p, d=d, m=m, edge=edge,
        learning_progress=engine.learning_progress(attempts), attempts=len(attempts),
        learned=len(attempts) >= 1, mastered=engine.concept_mastered(mastery, len(attempts), eff_min),
        due_for_review=engine.is_due_for_review(mastery, m),
    )


def concept_state(db: Session, learner_id: uuid.UUID, node: models.KnowledgeNode,
                  now: datetime | None = None) -> ConceptState:
    now = now or engine.now_utc()
    return _build_concept_state(node, concept_attempts(db, learner_id, node.id),
                                _concept_item_count(db, node.id), now)


def concept_states_batch(db: Session, learner_id: uuid.UUID, nodes,
                         now: datetime | None = None) -> dict[str, ConceptState]:
    """concept_state for a whole list of concepts in 2 queries total (attempts + counts) instead of
    2 per concept. Returns {node_id -> ConceptState}."""
    now = now or engine.now_utc()
    ids = [n.id for n in nodes]
    attempts_map = concept_attempts_batch(db, learner_id, ids)
    counts_map = concept_item_counts_batch(db, ids)
    return {n.id: _build_concept_state(n, attempts_map.get(n.id, []), counts_map.get(n.id, 0), now)
            for n in nodes}


# ---------- structure ----------
def _concepts_of_topic(db: Session, topic_id: str) -> list[models.KnowledgeNode]:
    return db.scalars(
        select(models.KnowledgeNode)
        .where(models.KnowledgeNode.parent_id == topic_id,
               models.KnowledgeNode.kind == models.NodeKind.concept.value)
        .order_by(models.KnowledgeNode.id.asc())
    ).all()


def _prereqs_of(db: Session, topic_id: str) -> list[str]:
    return list(db.scalars(
        select(models.PrereqEdge.prereq_node_id).where(models.PrereqEdge.node_id == topic_id)
    ).all())


def topics_of(db: Session, exam: str, section_key: str | None = None) -> list[models.KnowledgeNode]:
    q = (select(models.KnowledgeNode)
         .where(models.KnowledgeNode.exam_code == exam,
                models.KnowledgeNode.kind == models.NodeKind.topic.value))
    if section_key:
        sec = db.scalar(select(models.Section).where(
            models.Section.exam_code == exam, models.Section.key == section_key))
        if sec is None:
            return []
        q = q.where(models.KnowledgeNode.section_id == sec.id)
    return db.scalars(q.order_by(models.KnowledgeNode.id.asc())).all()


def _section_key_of(db: Session, node: models.KnowledgeNode) -> str:
    sec = db.get(models.Section, node.section_id)
    return sec.key if sec else ""


def topic_mastery(db: Session, learner_id: uuid.UUID, topic: models.KnowledgeNode,
                  now: datetime | None = None) -> float:
    concepts = _concepts_of_topic(db, topic.id)
    if not concepts:
        return 0.0
    now = now or engine.now_utc()
    states = concept_states_batch(db, learner_id, concepts, now)
    return sum(states[c.id].mastery for c in concepts) / len(concepts)


def is_topic_locked(db: Session, learner_id: uuid.UUID, topic: models.KnowledgeNode,
                    now: datetime | None = None) -> bool:
    if not settings.zpd_use_prereqs:
        return False
    prereqs = _prereqs_of(db, topic.id)
    if not prereqs:
        return False
    now = now or engine.now_utc()
    for pid in prereqs:
        p_topic = db.get(models.KnowledgeNode, pid)
        if p_topic is not None and topic_mastery(db, learner_id, p_topic, now) < engine.H:
            return True
    return False


def topic_view(db: Session, learner_id: uuid.UUID, topic: models.KnowledgeNode,
               now: datetime | None = None) -> TopicView:
    now = now or engine.now_utc()
    concept_nodes = _concepts_of_topic(db, topic.id)
    states = concept_states_batch(db, learner_id, concept_nodes, now)
    concepts = [states[c.id] for c in concept_nodes]
    mastery = sum(c.mastery for c in concepts) / len(concepts) if concepts else 0.0
    return TopicView(
        node_id=topic.id, name=topic.name, section_key=_section_key_of(db, topic),
        locked=is_topic_locked(db, learner_id, topic, now), mastery=mastery,
        prereqs=_prereqs_of(db, topic.id), concepts=concepts,
    )


# ---------- the bandits ----------
def _approved_clause(q):
    """Apply the approved-only filter unless drafts are being served (testing)."""
    return q.where(models.Item.status == "approved") if settings.serve_only_approved else q


def _concept_has_items(db: Session, concept_id: str) -> bool:
    """True if the concept has at least one servable item (approved, unless drafts are enabled)."""
    return db.scalar(_approved_clause(
        select(models.Item.item_id).where(models.Item.concept_node_id == concept_id)
    ).limit(1)) is not None


def _concept_item_count(db: Session, concept_id: str) -> int:
    """Number of servable items in a concept — used to cap the mastery min-attempts so a concept with
    fewer items than the global floor is still masterable."""
    return len(db.scalars(_approved_clause(
        select(models.Item.item_id).where(models.Item.concept_node_id == concept_id)
    )).all())


def _topic_has_items(db: Session, topic_id: str) -> bool:
    """True if any concept under this topic has a servable item."""
    sub = select(models.KnowledgeNode.id).where(models.KnowledgeNode.parent_id == topic_id)
    return db.scalar(_approved_clause(
        select(models.Item.item_id).where(models.Item.concept_node_id.in_(sub))
    ).limit(1)) is not None


def topic_fully_mastered(db: Session, learner_id: uuid.UUID, topic: models.KnowledgeNode,
                         now: datetime | None = None) -> bool:
    """True when every servable concept under the topic is mastered (the attempts-gated flag) — i.e.
    there is nothing left to teach *or climb* here. Distinct from ``topic_mastery >= H``, which can be
    true after a single lucky correct while harder items remain unseen."""
    now = now or engine.now_utc()
    concepts = [c for c in _concepts_of_topic(db, topic.id)
                if _concept_has_items(db, c.id) and not is_practice_bank(c)]
    if not concepts:
        return True
    return all(concept_state(db, learner_id, c, now).mastered for c in concepts)


def recommend_topics_ranked(db: Session, learner_id: uuid.UUID, exam: str,
                            section_key: str | None = None, now: datetime | None = None):
    """Topic bandit, full ranking: UNLOCKED topics that still have something to teach or climb, with
    servable items, ordered by expected learning gain (best first). A topic stays in play until its
    concepts are *mastered* (worked up the difficulty ladder), not merely above H."""
    now = now or engine.now_utc()
    scored = []
    for topic in topics_of(db, exam, section_key):
        if is_topic_locked(db, learner_id, topic, now):
            continue
        if not _topic_has_items(db, topic.id):
            continue
        if topic_fully_mastered(db, learner_id, topic, now):
            continue
        m = topic_mastery(db, learner_id, topic, now)
        started = any(concept_state(db, learner_id, c, now).learned
                      for c in _concepts_of_topic(db, topic.id))
        scored.append((engine.expected_gain(m, started), topic))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [t for _, t in scored]


def recommend_topic(db: Session, learner_id: uuid.UUID, exam: str,
                    section_key: str | None = None, now: datetime | None = None):
    ranked = recommend_topics_ranked(db, learner_id, exam, section_key, now)
    return ranked[0] if ranked else None


def node_prereq_detail(db: Session, learner_id: uuid.UUID, node: models.KnowledgeNode,
                       now: datetime | None = None) -> list[dict]:
    """The ZPD calculation, made visible: each prerequisite of `node` with the learner's current
    mastery of it and whether it clears the H threshold. Empty list = no prerequisites."""
    now = now or engine.now_utc()
    out = []
    for pid in _prereqs_of(db, node.id):
        pnode = db.get(models.KnowledgeNode, pid)
        if pnode is None:
            continue
        m = (concept_state(db, learner_id, pnode, now).mastery
             if pnode.kind == models.NodeKind.concept.value
             else topic_mastery(db, learner_id, pnode, now))
        out.append({"id": pid, "name": pnode.name, "mastery": round(m, 4), "met": m >= engine.H})
    return out


def is_concept_locked(db: Session, learner_id: uuid.UUID, concept: models.KnowledgeNode,
                      now: datetime | None = None) -> bool:
    """A concept is locked until all its prerequisite nodes are mastered. Prereqs may be other
    concepts (use concept mastery) or topics (use topic mastery). Matches the template's
    subtopic-level Prerequisites column."""
    if not settings.zpd_use_prereqs:
        return False
    prereqs = _prereqs_of(db, concept.id)
    if not prereqs:
        return False
    now = now or engine.now_utc()
    for pid in prereqs:
        pnode = db.get(models.KnowledgeNode, pid)
        if pnode is None:
            continue
        if pnode.kind == models.NodeKind.concept.value:
            if concept_state(db, learner_id, pnode, now).mastery < engine.H:
                return True
        elif topic_mastery(db, learner_id, pnode, now) < engine.H:
            return True
    return False


def _concepts_locked_batch(db: Session, learner_id: uuid.UUID, concepts,
                           now: datetime | None = None) -> dict[str, bool]:
    """Batched is_concept_locked for many concepts: resolve all prerequisite edges and the mastery
    of every referenced prereq in a handful of queries, instead of per-concept (which was an N+1
    that also recursed into concept_state / topic_mastery). Returns {concept_id -> locked?}."""
    ids = [c.id for c in concepts]
    if not settings.zpd_use_prereqs or not ids:
        return {}
    edges = db.execute(
        select(models.PrereqEdge.node_id, models.PrereqEdge.prereq_node_id)
        .where(models.PrereqEdge.node_id.in_(ids))).all()
    if not edges:
        return {}
    prereqs_by_concept: dict[str, list[str]] = defaultdict(list)
    prereq_ids: set[str] = set()
    for node_id, prereq_id in edges:
        prereqs_by_concept[node_id].append(prereq_id)
        prereq_ids.add(prereq_id)
    prereq_nodes = {n.id: n for n in db.scalars(
        select(models.KnowledgeNode).where(models.KnowledgeNode.id.in_(prereq_ids))).all()}
    # prereq mastery: concept prereqs via one batched state read; topic prereqs via topic_mastery.
    cp_states = concept_states_batch(
        db, learner_id, [n for n in prereq_nodes.values()
                         if n.kind == models.NodeKind.concept.value], now)
    tmastery = {n.id: topic_mastery(db, learner_id, n, now)
                for n in prereq_nodes.values() if n.kind == models.NodeKind.topic.value}

    def _mastered(pid: str) -> bool:
        n = prereq_nodes.get(pid)
        if n is None:
            return True  # dangling prereq — the per-concept version skipped it (didn't lock)
        if n.kind == models.NodeKind.concept.value:
            return (cp_states[pid].mastery if pid in cp_states else 1.0) >= engine.H
        return tmastery.get(pid, 0.0) >= engine.H

    return {cid: any(not _mastered(pid) for pid in prereqs_by_concept.get(cid, [])) for cid in ids}


def within_topic_candidates(db: Session, learner_id: uuid.UUID, topic: models.KnowledgeNode,
                            now: datetime | None = None, exclude_concept_ids=frozenset()):
    """All actionable concepts in a topic, in priority order: first the unlearned-and-unlocked ones
    to LEARN (node order), then the learned-but-below-H ones to REVISE (weakest first). Concepts
    with no servable items, locked concepts, and excluded concepts are dropped. Returns a list of
    (concept_node, mode)."""
    now = now or engine.now_utc()
    all_concepts = [c for c in _concepts_of_topic(db, topic.id) if not is_practice_bank(c)]
    have_items = concepts_with_items(db, [c.id for c in all_concepts])
    concepts = [c for c in all_concepts if c.id in have_items and c.id not in exclude_concept_ids]
    states = concept_states_batch(db, learner_id, concepts, now)
    locked = _concepts_locked_batch(db, learner_id, concepts, now)

    learn = [c for c in concepts if not states[c.id].learned and not locked.get(c.id)]
    revise = [c for c in concepts
              if states[c.id].learned and not states[c.id].mastered
              and not locked.get(c.id) and c not in learn]
    revise.sort(key=lambda c: states[c.id].mastery)
    return [(c, "learn") for c in learn] + [(c, "revise") for c in revise]


def within_topic_next(db: Session, learner_id: uuid.UUID, topic: models.KnowledgeNode,
                      now: datetime | None = None):
    """Head of within_topic_candidates: learn the next available concept, else revise the weakest.
    Returns (concept_node, mode) or (None, None)."""
    cands = within_topic_candidates(db, learner_id, topic, now)
    return cands[0] if cands else (None, None)
