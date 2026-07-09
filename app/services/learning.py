"""Learning orchestration — assembles the hierarchy into the 'what should I do next' decision.

Topic bandit (ZPD + room-to-grow) -> within-topic action (learn next / revise weakest)
-> problem bandit (Gaussian difficulty prior around the MAPLE edge) -> a concrete question.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from . import engine, knowledge_graph as kg
from .state import eligible_items, record_response

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)  # sort sentinel for never-seen items


def _exposure_detail(db: Session, learner_id: uuid.UUID) -> dict[str, tuple[int, datetime | None]]:
    """item_id -> (times_seen, last_seen_at) for this learner."""
    rows = db.execute(
        select(models.Exposure.item_id, models.Exposure.times_seen, models.Exposure.last_seen_at)
        .where(models.Exposure.learner_id == learner_id)
    ).all()
    return {iid: (seen, last) for iid, seen, last in rows}


def select_problem(db: Session, learner_id: uuid.UUID, concept_id: str, edge: float,
                   exclude_item_ids=frozenset(), allow_seen: bool = False) -> models.Item | None:
    """Problem bandit. Among practice-eligible items for the concept (minus excluded/skipped ones):
    serve a FRESH (never-answered) item whose difficulty sits nearest the learner's edge — so the
    learner never re-sees a question while unseen ones remain, and the difficulty served tracks the
    edge as it moves. Only when the fresh pool is exhausted *and* ``allow_seen`` is set do we
    resurface an already-answered item, choosing the least-recently-seen one (spaced review, never a
    tight repeat)."""
    candidates = [it for it in eligible_items(db, learner_id, context="practice", concept_node_id=concept_id)
                  if it.item_id not in exclude_item_ids]
    if not candidates:
        return None
    expo = _exposure_detail(db, learner_id)
    unseen = [it for it in candidates if expo.get(it.item_id, (0, None))[0] == 0]
    if unseen:
        # fresh items only: nearest the edge wins; item_id breaks ties for determinism
        unseen.sort(key=lambda it: (-engine.problem_weight(it.difficulty_d, edge), it.item_id))
        return unseen[0]
    if not allow_seen:
        return None  # fresh pool exhausted for this concept -> let next_step advance elsewhere
    # spaced review: least-recently-seen first, then fewest views, then nearest the edge
    candidates.sort(key=lambda it: (
        expo.get(it.item_id, (0, _EPOCH))[1] or _EPOCH,
        expo.get(it.item_id, (0, 0))[0],
        -engine.problem_weight(it.difficulty_d, edge),
    ))
    return candidates[0]


def practice_batch(db: Session, learner: models.Account, topic: models.KnowledgeNode,
                   limit: int = 1000, now: datetime | None = None) -> dict:
    """A batch of practice questions for one chapter, delivered as a set (like a sectional mock) so
    the learner can work a palette of questions instead of one-at-a-time. Ordering mirrors the
    problem bandit: FRESH (never-answered) items first, each near its concept's MAPLE edge; once the
    fresh pool is dry, already-seen items follow as spaced review. Difficulty/answer/solution are NOT
    included — feedback comes from POST /learn/answer after each submission."""
    now = now or engine.now_utc()
    concepts = kg._concepts_of_topic(db, topic.id)
    expo = _exposure_detail(db, learner.id)

    scored: list[tuple[int, float, str, models.Item]] = []
    for c in concepts:
        edge = kg.concept_state(db, learner.id, c, now).edge
        for it in eligible_items(db, learner.id, context="practice", concept_node_id=c.id):
            seen = expo.get(it.item_id, (0, None))[0]
            scored.append((0 if seen == 0 else 1,
                           -engine.problem_weight(it.difficulty_d, edge), it.item_id, it))
    scored.sort(key=lambda t: (t[0], t[1], t[2]))
    batch = [it for _, _, _, it in scored[:max(1, min(limit, 2000))]]

    return {
        "exam": topic.exam_code,
        "chapter": {"id": topic.id, "name": topic.name, "section": kg._section_key_of(db, topic)},
        "questions": [{
            "item_id": it.item_id, "stem": it.stem, "options": it.options or [],
            "format": it.format, "num_options": it.num_options,
            "difficulty": it.difficulty_d, "concept_id": it.concept_node_id,
        } for it in batch],
    }


def next_step(db: Session, learner: models.Account, exam: str,
              section_key: str | None = None, now: datetime | None = None,
              exclude_item_ids=frozenset()) -> dict:
    """Walk topics by expected gain; within each, walk candidate concepts (learn then revise) and
    serve a question. **Pass 1** serves only FRESH (never-answered) items, so while any unseen
    question remains the learner never sees a repeat and difficulty tracks the moving edge. Only when
    every fresh question is exhausted does **Pass 2** resurface earlier items as spaced review.
    ``exclude_item_ids`` additionally skips anything shown/skipped this session."""
    now = now or engine.now_utc()
    topics = kg.recommend_topics_ranked(db, learner.id, exam, section_key, now)

    def _walk(allow_seen: bool, review: bool):
        for topic in topics:
            for concept_node, mode in kg.within_topic_candidates(db, learner.id, topic, now):
                cstate = kg.concept_state(db, learner.id, concept_node, now)
                item = select_problem(db, learner.id, concept_node.id, cstate.edge,
                                      exclude_item_ids, allow_seen=allow_seen)
                if item is None:
                    continue
                served_mode = "review" if review else mode
                if review:
                    rationale = (f"All fresh questions in '{topic.name}' are done — this is spaced "
                                 f"review of an earlier item (difficulty {item.difficulty_d}).")
                else:
                    rationale = (f"ZPD: '{topic.name}' is unlocked and the highest expected-gain topic; "
                                 f"{'teaching a new concept' if mode == 'learn' else 'revising your weakest concept'}; "
                                 f"difficulty {item.difficulty_d} chosen near your level (edge {cstate.edge:.1f}).")
                payload = {
                    "status": "ok",
                    "section": kg._section_key_of(db, topic),
                    "topic": {"id": topic.id, "name": topic.name},
                    "concept": {"id": concept_node.id, "name": concept_node.name},
                    "mode": served_mode,  # "learn" | "revise" | "review"
                    "rationale": rationale,
                    "question": {
                        "item_id": item.item_id, "stem": item.stem, "options": item.options,
                        "format": item.format, "num_options": item.num_options,
                    },
                }
                if served_mode == "learn" and cstate.attempts == 0:
                    payload["theory"] = concept_node.theory
                return payload
        return None

    served = _walk(allow_seen=False, review=False)   # fresh only -> zero repeats
    if served:
        return served
    served = _walk(allow_seen=True, review=True)      # exhausted -> spaced review
    if served:
        return served

    if topics:
        return {"status": "done",
                "message": "You've worked through every available question here. Add more questions, "
                           "or come back later once items are due for review."}
    return {"status": "done",
            "message": "No unlocked, unmastered topics here — section complete or everything is locked."}


def answer(db: Session, learner: models.Account, item: models.Item, *, answer_given: str | None,
           response_time_ms: int | None, session_id: str | None, now: datetime | None = None) -> dict:
    now = now or engine.now_utc()
    _resp, correct, state = record_response(
        db, learner, item, context="practice", answer_given=answer_given, correct=None,
        response_time_ms=response_time_ms, attempt_number=1, hints_used=0, session_id=session_id,
    )
    bs = state.bandit_state or {}
    return {
        "correct": correct,
        "solution": item.solution,
        "correct_answer": item.correct_answer,
        "concept": item.concept_node_id,
        "mastery": round(state.mastery, 4),
        "breakdown": {"P": round(state.performance_p, 4), "D": round(state.difficulty_score, 4),
                      "M": round(state.memory_strength, 4)},
        "edge": round(bs.get("edge", engine.MAPLE_START), 2),
        "mastered": bool(bs.get("mastered", False)),
        "due_for_review": bool(bs.get("due_for_review", False)),
        "attempts": int(bs.get("attempts", 0)),
    }


def learning_map(db: Session, learner: models.Account, exam: str,
                 section_key: str | None = None, now: datetime | None = None) -> dict:
    now = now or engine.now_utc()
    recommended = kg.recommend_topic(db, learner.id, exam, section_key, now)
    rec_id = recommended.id if recommended else None

    topics_out = []
    for topic in kg.topics_of(db, exam, section_key):
        tv = kg.topic_view(db, learner.id, topic, now)
        topics_out.append({
            "id": tv.node_id, "name": tv.name, "section": tv.section_key,
            "locked": tv.locked, "mastery": round(tv.mastery, 4),
            "recommended": tv.node_id == rec_id,
            "prereqs_detail": kg.node_prereq_detail(db, learner.id, topic, now),
            "concepts": [_concept_map_entry(db, learner.id, c, now) for c in tv.concepts],
        })
    return {"exam": exam, "section": section_key, "recommended_topic": rec_id,
            "mastery_threshold": round(engine.H, 4), "topics": topics_out}


def _concept_map_entry(db: Session, learner_id: uuid.UUID, c, now) -> dict:
    cn = db.get(models.KnowledgeNode, c.node_id)
    return {
        "id": c.node_id, "name": c.name, "mastery": round(c.mastery, 4),
        "learned": c.learned, "mastered": c.mastered, "due_for_review": c.due_for_review,
        "P": round(c.p, 4), "D": round(c.d, 4), "M": round(c.m, 4),
        "edge": round(c.edge, 2), "attempts": c.attempts,
        "locked": kg.is_concept_locked(db, learner_id, cn, now),
        "prereqs_detail": kg.node_prereq_detail(db, learner_id, cn, now),
    }


def due_reviews(db: Session, learner: models.Account, exam: str, now: datetime | None = None) -> dict:
    now = now or engine.now_utc()
    due = []
    for topic in kg.topics_of(db, exam):
        for c in kg._concepts_of_topic(db, topic.id):
            cs = kg.concept_state(db, learner.id, c, now)
            if cs.due_for_review:
                due.append({"concept_id": cs.node_id, "name": cs.name, "topic": topic.name,
                            "mastery": round(cs.mastery, 4), "memory": round(cs.m, 4)})
    return {"exam": exam, "due": due}
