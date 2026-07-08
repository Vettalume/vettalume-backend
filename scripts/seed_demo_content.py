"""Seed the learning content tree (sections -> chapters -> subtopics) for CAT / GMAT / GRE to match
the frontend's demo structure, so the student app is fully populated AND fully admin-editable.

    python -m scripts.seed_demo_content

Reseeds content only (sections, knowledge_nodes, and everything depending on them). Accounts, auth and
admins are untouched. Each chapter is a `topic`; each chapter gets 3 `concept` subtopics with a theory
body + a placeholder video so the learning page has something to show. Admins can then edit/replace.
"""
from __future__ import annotations

import hashlib
import re
import uuid

from app.db import SessionLocal, Base
from app import models  # noqa: F401  (register mappers)


def slug(s: str) -> str:
    return re.sub(r"(^-|-$)", "", re.sub(r"[^a-z0-9]+", "-", (s or "").lower().replace("&", "and")))


# exam code -> exam name -> section key -> (section name, [chapter names])
STRUCTURE = {
    "CAT": {
        "name": "Common Admission Test",
        "sections": {
            "VARC": ("Verbal Ability & Reading Comprehension",
                     ["Main Idea & Structure", "Tone & Attitude", "Vocabulary in Context",
                      "Para Jumbles", "Para Summary", "Odd Sentence Out"]),
            "DILR": ("Data Interpretation & Logical Reasoning",
                     ["Tables & Charts", "Caselets", "Venn Diagrams",
                      "Arrangements", "Puzzles", "Blood Relations"]),
            "QA": ("Quantitative Ability",
                   ["Percentages", "Ratio & Proportion", "Time & Work",
                    "Equations", "Functions", "Progressions"]),
        },
    },
    "GMAT": {
        "name": "GMAT",
        "sections": {
            "QUANT": ("Quantitative Reasoning",
                      ["Arithmetic", "Algebra", "Geometry",
                       "Number Properties", "Rates & Work", "Combinatorics"]),
            "VERBAL": ("Verbal Reasoning",
                       ["Main Idea", "Inference", "Tone",
                        "Assumptions", "Strengthen / Weaken", "Evaluate"]),
            "DATA": ("Data Insights",
                     ["Two-Statement Logic", "Value vs Yes/No", "Common Traps",
                      "Table Analysis", "Graphics Interpretation", "Two-Part Analysis"]),
        },
    },
    "GRE": {
        "name": "GRE",
        "sections": {
            "VERBAL": ("Verbal Reasoning",
                       ["Main Idea", "Inference", "Vocabulary in Context",
                        "One-Blank", "Two-Blank", "Three-Blank"]),
            "QUANT": ("Quantitative Reasoning",
                      ["Arithmetic", "Algebra", "Word Problems",
                       "Geometry", "Data Interpretation", "Probability"]),
            "AWA": ("Analytical Writing",
                    ["Thesis & Structure", "Evidence", "Style & Clarity",
                     "Flaw Identification", "Counterexamples", "Conclusion"]),
        },
    },
}

SUBTOPICS = ["Core Concepts", "Worked Examples", "Advanced Practice"]


def content_tables_in_delete_order():
    """Tables that depend (transitively) on sections/knowledge_nodes/items, child-first for safe delete."""
    meta = Base.metadata
    dependents = {"sections", "knowledge_nodes", "items"}
    changed = True
    while changed:
        changed = False
        for t in meta.sorted_tables:
            for fk in t.foreign_keys:
                if fk.column.table.name in dependents and t.name not in dependents:
                    dependents.add(t.name)
                    changed = True
    return [t for t in reversed(meta.sorted_tables) if t.name in dependents]


def run(db) -> dict:
    """Wipe + reseed content using the given session. Callable from a script (new connection) OR from
    inside the running app (warm pooled connection) — the latter dodges Neon control-plane throttling."""
    # 1) wipe existing content (child tables first) — accounts/auth/admins are not touched
    for t in content_tables_in_delete_order():
        db.execute(t.delete())
    db.commit()

    n_sec = n_ch = n_sub = n_q = 0
    for code, ex in STRUCTURE.items():
        if db.get(models.Exam, code) is None:
            db.add(models.Exam(code=code, name=ex["name"]))
            db.flush()
        for skey, (sname, chapters) in ex["sections"].items():
            section = models.Section(id=uuid.uuid4(), exam_code=code, key=skey, name=sname)
            db.add(section)
            db.flush()
            n_sec += 1

            def tid_of(ch: str) -> str:
                return f"{code.lower()}-{skey.lower()}-{slug(ch)}"

            # pass 1: chapters (topic nodes) — flush so concepts can reference them
            for chapter in chapters:
                db.add(models.KnowledgeNode(
                    id=tid_of(chapter), exam_code=code, section_id=section.id,
                    kind=models.NodeKind.topic.value, name=chapter,
                ))
                n_ch += 1
            db.flush()

            # pass 2: subtopics (concept nodes) — flush so items can reference them
            for chapter in chapters:
                for sub in SUBTOPICS:
                    cid = f"{tid_of(chapter)}-{slug(sub)}"
                    body = (f"<h2>{sub}</h2>"
                            f"<p><b>{sub}</b> for the <b>{chapter}</b> chapter "
                            f"({sname}). This is seed content — an admin can replace the theory, "
                            f"add videos and quiz questions from the content portal.</p>")
                    db.add(models.KnowledgeNode(
                        id=cid, exam_code=code, section_id=section.id,
                        kind=models.NodeKind.concept.value, name=sub, parent_id=tid_of(chapter),
                        theory={"body": body,
                                "videos": [{"title": f"{chapter}: {sub}", "url": "", "duration": "08:24"}]},
                    ))
                    n_sub += 1
            db.flush()

            # pass 3: quiz questions (items) — concepts now exist, so the FK holds
            for chapter in chapters:
                for sub in SUBTOPICS:
                    cid = f"{tid_of(chapter)}-{slug(sub)}"
                    for qi in range(1, 4):
                        stem = (f"Practice question {qi} on {sub} ({chapter}). "
                                f"Which of the following statements is correct?")
                        opts = [f"Option {chr(64 + k)}" for k in range(1, 5)]
                        correct = opts[(qi - 1) % 4]
                        db.add(models.Item(
                            item_id=f"{cid}-q{qi}",
                            content_hash=hashlib.md5(stem.encode()).hexdigest(),
                            exam_code=code, section_id=section.id, concept_node_id=cid,
                            difficulty_d=((qi - 1) % 3) + 1,
                            format=models.ItemFormat.mcq.value, num_options=4,
                            stem=stem, options=opts, correct_answer=correct,
                            solution=f"The correct answer is {correct}. (Seed explanation for {sub}.)",
                            usage_scope=models.UsageScope.both.value, status="approved",
                        ))
                        n_q += 1
            db.flush()
    db.commit()
    return {"sections": n_sec, "chapters": n_ch, "subtopics": n_sub, "questions": n_q}


def main() -> None:
    db = SessionLocal()
    counts = run(db)
    db.close()
    print(f"seeded: {counts['sections']} sections, {counts['chapters']} chapters, "
          f"{counts['subtopics']} subtopics, {counts['questions']} quiz questions")


if __name__ == "__main__":
    main()
