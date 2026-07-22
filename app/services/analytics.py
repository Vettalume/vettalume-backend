"""Per-chapter analytics — the numbers behind the chapter dashboard.

A 'chapter' is a topic node; its 'subtopics' are the concept nodes under it. Everything here is
derived from the append-only Response spine plus the live node states, so it reflects exactly what
the learner has done. No new tables: this is pure read-side aggregation.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from ..models import MOCK_CONTEXTS
from . import engine, knowledge_graph as kg

# Difficulty bands D1..D5 == authored difficulty -2..2
_BANDS = [(-2, "D1"), (-1, "D2"), (0, "D3"), (1, "D4"), (2, "D5")]

# A subtopic counts as "learnt" once its MAB mastery crosses this bar. Deliberately a touch below
# the "mastered" bar (engine.H = 0.74, which is attempts-gated): "learnt" = you can do it,
# "mastered" = you've proven it across the difficulty ladder.
LEARNT_THRESHOLD = 0.70



def resolve_chapter(db: Session, exam: str, topic: str | None, topic_id: str | None):
    """Find the topic node by id (preferred) or case-insensitive name within the exam."""
    if topic_id:
        n = db.get(models.KnowledgeNode, topic_id)
        return n if (n and n.kind == models.NodeKind.topic.value and n.exam_code == exam) else None
    if topic:
        for t in kg.topics_of(db, exam):
            if t.name.lower() == topic.strip().lower():
                return t
    return None


def chapter_analysis(db: Session, learner: models.Account, topic: models.KnowledgeNode,
                     now: datetime | None = None) -> dict:
    now = now or engine.now_utc()
    concepts = kg._concepts_of_topic(db, topic.id)          # incl. the hidden practice bank
    concept_ids = [c.id for c in concepts]                  # aggregates count practice too
    # subtopic-level DISPLAY (list, strongest/weakest, topics-learnt) excludes the practice bank —
    # it isn't a real subtopic — but its responses still feed the chapter's totals/accuracy/mastery.
    display_concepts = [c for c in concepts if not kg.is_practice_bank(c)]
    name_by_id = {c.id: c.name for c in concepts}

    # ---- pull every response in this chapter, oldest first ----
    rows = db.execute(
        select(models.Response, models.Item.concept_node_id)
        .join(models.Item, models.Response.item_id == models.Item.item_id)
        .where(models.Response.learner_id == learner.id,
               models.Item.concept_node_id.in_(concept_ids or ["__none__"]))
        .order_by(models.Response.created_at)
    ).all()
    all_responses = [(r, cid) for r, cid in rows]
    # The learner-facing "questions answered / accuracy / difficulty" numbers count LEARNING work
    # only — i.e. the subtopic quizzes and topic practice (both context="practice"), which come from
    # the same item pool. Scored-mock responses have their own analysis and are excluded here.
    responses = [(r, cid) for r, cid in all_responses
                 if r.context == models.Context.practice.value]
    total = len(responses)
    total_correct = sum(1 for r, _ in responses if r.correct)

    # ---- per-concept live state (mastery / learned / edge) ----
    cstates = kg.concept_states_batch(db, learner.id, concepts, now)  # 2 queries, was 2 per concept
    # topic mastery = mean concept mastery — reuse cstates instead of kg.topic_mastery re-fetching it.
    topic_mastery = (sum(cstates[c.id].mastery for c in concepts) / len(concepts)) if concepts else 0.0
    # "Topics learnt" = subtopics whose MAB mastery has crossed the learnt bar (70%).
    learnt = sum(1 for c in display_concepts if cstates[c.id].mastery >= LEARNT_THRESHOLD)

    # ---- KPIs ----
    kpis = {
        "questions_answered": total,
        "topic_mastery": round(topic_mastery, 4),
        "concepts_learnt": learnt,
        "concepts_total": len(display_concepts),
        "overall_accuracy": round(total_correct / total, 4) if total else 0.0,
    }

    # ---- difficulty spread: accuracy per band ----
    by_band_total = defaultdict(int)
    by_band_correct = defaultdict(int)
    for r, _ in responses:
        by_band_total[r.difficulty_d] += 1
        by_band_correct[r.difficulty_d] += 1 if r.correct else 0
    difficulty_spread = [{
        "band": label, "d": d,
        "answered": by_band_total[d],
        "correct": by_band_correct[d],
        "cleared_pct": round(by_band_correct[d] / by_band_total[d], 4) if by_band_total[d] else 0.0,
    } for d, label in _BANDS]

    # ---- improvement over time: weekly cumulative accuracy proxy ----
    improvement = _improvement_over_time(responses, topic_mastery, now)

    # strongest / weakest topics are computed further down, once _progress is defined — they rank by
    # (and display) the same progress value the subtopic list shows, so the numbers agree.

    # ---- per-concept accuracy (for subtopics + recommendations) ----
    c_total = defaultdict(int)
    c_correct = defaultdict(int)
    for r, cid in responses:
        c_total[cid] += 1
        c_correct[cid] += 1 if r.correct else 0
    def _acc(cid):
        return round(c_correct[cid] / c_total[cid], 4) if c_total[cid] else 0.0

    # ---- per-concept progress = 25% notes + 25% video + 50% (distinct correct / total questions).
    # Same blend the section page / dashboard use, so a subtopic's % means "how far through it you
    # are", not accuracy on the few questions attempted. ----
    item_counts = dict(db.execute(
        select(models.Item.concept_node_id, func.count())
        .where(models.Item.concept_node_id.in_(concept_ids or ["__none__"]))
        .group_by(models.Item.concept_node_id)
    ).all())
    correct_items_by_concept: dict[str, set] = defaultdict(set)
    for r, cid in responses:
        if r.correct:
            correct_items_by_concept[cid].add(r.item_id)
    eng_by_concept = {
        s.node_id: (s.engagement or {})
        for s in db.scalars(select(models.LearnerNodeState).where(
            models.LearnerNodeState.learner_id == learner.id,
            models.LearnerNodeState.node_id.in_(concept_ids or ["__none__"]))).all()
    }

    def _progress(cid: str) -> float:
        eng = eng_by_concept.get(cid, {})
        read = 1.0 if eng.get("read") else 0.0
        watched = 1.0 if eng.get("watched") else 0.0
        tot = item_counts.get(cid, 0)
        quiz = (len(correct_items_by_concept.get(cid, set())) / tot) if tot else 0.0
        return round(0.25 * read + 0.25 * watched + 0.50 * quiz, 4)

    # ---- strongest / weakest subtopics ranked by PROGRESS (matches the subtopic list values) ----
    def _entry(c):
        return {"id": c.id, "name": c.name,
                "progress": _progress(c.id), "mastery": round(cstates[c.id].mastery, 4)}
    strongest = sorted((_entry(c) for c in display_concepts),
                       key=lambda e: e["progress"], reverse=True)[:5]
    weakest = sorted((_entry(c) for c in display_concepts), key=lambda e: e["progress"])[:5]

    subtopics = [{
        "id": c.id, "name": c.name,
        "progress": _progress(c.id),
        "mastery": round(cstates[c.id].mastery, 4),
        "learned": cstates[c.id].learned, "mastered": cstates[c.id].mastered,
        "learnt": cstates[c.id].mastery >= LEARNT_THRESHOLD,
        "attempts": cstates[c.id].attempts, "accuracy": _acc(c.id),
        "edge": round(cstates[c.id].edge, 2),
    } for c in display_concepts]

    # ---- recommended next actions (driven by the same MAB the loop uses) ----
    actions = _recommended_actions(db, learner, topic, cstates, _acc, now)

    # ---- topic practice test: last mock-context batch in this chapter ----
    practice_test = _last_practice_test(all_responses)

    return {
        "exam": topic.exam_code,
        "chapter": {"id": topic.id, "name": topic.name,
                    "section": kg._section_key_of(db, topic)},
        "kpis": kpis,
        "difficulty_spread": difficulty_spread,
        "improvement_over_time": improvement,
        "strongest": strongest,
        "weakest": weakest,
        "recommended_actions": actions,
        "practice_test": practice_test,
        "mastery_threshold": round(engine.H, 4),
        "learnt_threshold": LEARNT_THRESHOLD,
        "subtopics": subtopics,
    }


def chapter_attempts(db: Session, learner: models.Account, topic: models.KnowledgeNode) -> dict:
    """Every question the learner has solved in this chapter through the subtopic quizzes and topic
    practice (context="practice"), newest attempt per item, so they can revisit what they answered —
    their answer, whether it was right, the correct answer, and the worked solution."""
    concepts = kg._concepts_of_topic(db, topic.id)
    concept_ids = [c.id for c in concepts]
    name_by_id = {c.id: c.name for c in concepts}

    rows = db.execute(
        select(models.Response, models.Item)
        .join(models.Item, models.Response.item_id == models.Item.item_id)
        .where(models.Response.learner_id == learner.id,
               models.Item.concept_node_id.in_(concept_ids or ["__none__"]),
               models.Response.context == models.Context.practice.value)
        .order_by(models.Response.created_at.desc())
    ).all()

    seen: set[str] = set()
    attempts_by_item: dict[str, int] = defaultdict(int)
    for r, it in rows:
        attempts_by_item[it.item_id] += 1
    out = []
    for r, it in rows:  # newest first; keep only the latest attempt per item
        if it.item_id in seen:
            continue
        seen.add(it.item_id)
        out.append({
            "item_id": it.item_id,
            "concept_id": it.concept_node_id,
            "concept": name_by_id.get(it.concept_node_id, ""),
            "format": it.format,
            "difficulty": it.difficulty_d,
            "stem": it.stem,
            "options": it.options or [],
            "correct_answer": it.correct_answer,
            "solution": it.solution or "",
            "your_answer": r.answer_given,
            "correct": bool(r.correct),
            "attempts": attempts_by_item[it.item_id],
            "answered_at": r.created_at.isoformat(),
        })
    out.reverse()  # chronological for display (first-solved first)
    return {"exam": topic.exam_code,
            "chapter": {"id": topic.id, "name": topic.name, "section": kg._section_key_of(db, topic)},
            "attempts": out}


def _improvement_over_time(responses, current_mastery, now, buckets: int = 8) -> dict:
    """How the learner's correctness climbed. If activity spans >= 2 weeks we bucket by ISO week;
    otherwise (fresh data) we bucket by progress through the questions, so the curve is still
    meaningful. Cumulative correctness rate is a cheap, honest proxy for mastery growth."""
    if not responses:
        return {"series": [], "delta": 0.0, "current": round(current_mastery, 4), "unit": "step"}

    span_days = (responses[-1][0].created_at - responses[0][0].created_at).days

    series = []
    if span_days >= 14:
        start = now - timedelta(weeks=buckets - 1)
        run_t = run_c = 0
        ptr = 0
        for w in range(buckets):
            wk_end = start + timedelta(weeks=w + 1)
            while ptr < len(responses) and responses[ptr][0].created_at <= wk_end:
                run_t += 1
                run_c += 1 if responses[ptr][0].correct else 0
                ptr += 1
            series.append({"label": f"W{w + 1}", "mastery": round((run_c / run_t) if run_t else 0.0, 4)})
        unit = "week"
    else:
        n = len(responses)
        seg = max(1, -(-n // buckets))  # ceil(n / buckets)
        run_c = 0
        idx = 0
        b = 0
        for i, (r, _) in enumerate(responses, start=1):
            run_c += 1 if r.correct else 0
            if i % seg == 0 or i == n:
                b += 1
                series.append({"label": f"Q{i}", "mastery": round(run_c / i, 4)})
        unit = "step"

    first = series[0]["mastery"] if series else 0.0
    last = series[-1]["mastery"] if series else 0.0
    return {"series": series, "delta": round(last - first, 4), "current": round(last, 4), "unit": unit}


def _recommended_actions(db, learner, topic, cstates, acc_fn, now) -> list[dict]:
    actions: list[dict] = []
    cands = kg.within_topic_candidates(db, learner.id, topic, now)
    learn = [c for c, mode in cands if mode == "learn"]
    if learn:
        actions.append({"action": "learn", "concept_id": learn[0].id, "concept": learn[0].name,
                        "text": f"Learn {learn[0].name} next — start with easier items, then push harder."})
    # weakest learned concepts below threshold -> revise
    revise = sorted([c for c, mode in cands if mode == "revise"],
                    key=lambda c: cstates[c.id].mastery)
    for c in revise[:2]:
        actions.append({"action": "revise", "concept_id": c.id, "concept": c.name,
                        "text": f"Revisit {c.name} — mastery {round(cstates[c.id].mastery*100)}%, "
                                f"accuracy {round(acc_fn(c.id)*100)}%."})
    if not actions:
        actions.append({"action": "done", "concept": None,
                        "text": "Every concept in this chapter is mastered — try a mixed practice test."})
    return actions


def _last_practice_test(responses) -> dict:
    """Most recent scored-mock batch (by session) within this chapter, if any."""
    sessions = defaultdict(list)
    for r, _ in responses:
        if r.context in MOCK_CONTEXTS and r.session_id:
            sessions[r.session_id].append(r)
    if not sessions:
        return {"last_correct": 0, "last_total": 0, "last_accuracy": 0.0}
    # pick the session whose latest response is newest
    sid = max(sessions, key=lambda s: max(x.created_at for x in sessions[s]))
    batch = sessions[sid]
    correct = sum(1 for x in batch if x.correct)
    return {"last_correct": correct, "last_total": len(batch),
            "last_accuracy": round(correct / len(batch), 4) if batch else 0.0}
