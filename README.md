

# Vettalume Backend

The unified backend for Vettalume (CAT / GMAT / GRE) — "one brain, three skins."
**Stack:** Python · FastAPI · SQLAlchemy 2.0 · PostgreSQL · (Redis wired for later phases).

This repository is being built in phases. **Phase 0 (this drop)** is the walking skeleton:
the locked data model, the bulk-ingestion QC gate, and a proven end-to-end path
(ingest items → answer → state persists).

---

## Run it

### Option A — Docker (Postgres + Redis + app)
```bash
docker compose up --build
```
Then open:
- **http://localhost:8000/docs** — interactive API (Swagger). This is your Phase-0 test surface.
- **http://localhost:8000/dashboard** — the Vettalume dashboard prototype (reference; wired up in Phase 1).

### Option B — Local, zero infra (SQLite)  ← recommended to start
Use **Python 3.9–3.13**. Do **not** use Python 3.14 yet — it's too new and several
dependencies don't ship prebuilt wheels for it, so installs try to compile from source and fail.
`psycopg2` is **not** needed here (it's Postgres-only).

```bash
# pick a Python that is NOT 3.14 (your system /usr/bin/python3 is usually fine):
/usr/bin/python3 --version
/usr/bin/python3 -m venv .venv
source .venv/bin/activate
python --version                       # sanity-check: must not say 3.14
pip install --upgrade pip
pip install -r requirements.txt        # lean: no psycopg2, nothing to compile
uvicorn app.main:app --reload          # SQLite is the default — no DATABASE_URL needed
```

Then open **http://localhost:8000/docs**. (SQLite writes to `./vettalume.db`. To use Postgres
instead, set `DATABASE_URL=postgresql+psycopg2://…`; Docker does this for you.)

> On macOS, if `python3` points to 3.14 (Homebrew/python.org), either use `/usr/bin/python3`
> as above, or install a stable one: `brew install python@3.12` and use `python3.12`.

---

## Test it

**Swagger (`/docs`)** is the fastest way to exercise every endpoint by hand. Typical flow:
1. `POST /auth/dev-login` with `{"email":"you@test.com"}` → copy the returned `learner_id`.
2. Click **Authorize**-style header use: pass `X-Learner-Id: <learner_id>` on the practice calls.
3. `GET /catalog/tree?exam=CAT` → see the seeded topic/concept tree.
4. `POST /ingest/items` → bulk-add questions (see schema in `/docs`).
5. `GET /practice/next?node_id=avg-simple` → get a question (answer is not returned).
6. `POST /practice/answer` → submit; correctness is graded server-side; mastery updates.
7. `GET /practice/state?exam=CAT` → see per-concept state.

**Scripted demo** (against a running server):
```bash
python scripts/demo_flow.py
```

**Automated smoke test** (in-memory SQLite, no server needed):
```bash
pip install -r requirements.txt
pytest -q
```

---

## Bulk upload — the authoring template (primary path)

`POST /ingest/question-bank/xlsx` accepts the **Vettalume question-bank template**
(`Vettalume_Question_Bank_Template.xlsx`). This is how authors add questions. One sheet does
three things at once:

1. **Builds the knowledge graph** from the `Topic` / `Subtopic` columns (topics and concepts are
   reused by name, so re-importing never duplicates them).
2. **Wires prerequisites** from the `Prerequisites` column (subtopic-level — e.g. *Weighted
   Averages requires Simple Averages*). These drive concept-level unlocking (ZPD).
3. **Ingests the questions**, resolving the answer to the option value.

The whole file is atomic: a bad row rejects the batch (taxonomy included) with a per-row error list.
A *dangling* prerequisite (a name that isn't a subtopic in the file) is reported as a **warning**, not
a rejection — the question still loads, only that one edge is skipped.

### Two accepted layouts

The importer reads **both** of these, including across multiple sheets in one upload:

* **Single sheet with an `Exam` column** (`Vettalume_Question_Bank_Template.xlsx`).
* **One sheet per exam** (`Vettalume_Question_Bank_by_Exam.xlsx`) — the **tab name is the exam**
  (CAT / GMAT / GRE), no `Exam` column needed. All tabs ingest in one atomic transaction.

It also handles the richer answer shapes automatically — the stored **format is derived from the
answer**, not from a label:

| In the sheet | Stored as |
|---|---|
| up to **5 options** (`Option A`–`Option E`) + a single letter | `mcq`, letter resolved to its value |
| **multiple letters** (`A, C` — e.g. Sentence Equivalence) | `tita`, the chosen option **values** joined |
| a **sequence / typed answer** with no options (`SPRQ`) | `tita`, the literal answer |
| `Question type` (Problem Solving, Data Sufficiency, Parajumble…) | kept on the item as `archetype_id` |


### What you author vs what the engine calculates later

| Tag (sheet column) | Who fills it |
|---|---|
| `Question ID`, `Exam`, `Section`, `Topic`, `Subtopic` | **author** |
| `Prerequisites` (subtopic names, or `-`) | **author** — becomes the prerequisite DAG |
| `Difficulty (-2 to 2)` | **author** — expert prior; also the seed for `IRT b` |
| `Question format` (`MCQ`/`TITA`), `Question text`, `Option A–D` | **author** |
| `Correct answer` (`A`/`B`/`C`/`D`, or the value) | **author** |
| `Solution / explanation`, `Expected time (sec)` | **author** |
| `Passage / Set ID` (groups questions sharing a passage, or `-`) | **author** |
| `Source`, `Status` (`approved` is served; others are held) | **author** |
| `IRT b` — *and its siblings `IRT a`, `IRT c`* | **derived by calibration (Phase 2)** — left empty on import |
| mastery, MAPLE edge, exposure, empirical p-values | **computed at runtime** — never stored as tags |

The `IRT b` column may sit in the sheet as a placeholder, but the importer **ignores it on
purpose**. Difficulty is on the same −2..2 scale as `IRT b` so your expert difficulty is literally
the prior that calibration refines from real response data.

> Import into a **fresh DB** the first time. If you import on top of the demo seed, nodes that share
> a name (e.g. *Simple Averages*) are reused rather than duplicated — which is correct, just be aware.

### Programmatic / generator path

`POST /ingest/items` (JSON) and `POST /ingest/items/xlsx` remain for the generator pipeline. Same
QC gate, same `-2..2` difficulty, and the same rule: **`irt_a` / `irt_b` / `irt_c` cannot be
authored** — the schema rejects them.

---

## Testing the MAB

Three ways to drive the engine, from most automated to most hands-on.

### 1. Deterministic drive (no server, no typing) — `scripts/mab_drive.py`

Imports a workbook into a throwaway in-memory DB and walks one learner through the Learning loop,
answering with a **controllable accuracy** (it reads each item's correct answer from the DB), then
prints the topic/concept/mode/difficulty/✓✗/mastery/MAPLE-edge per step and a final mastery map.

```bash
# approved-only (production behaviour)
python scripts/mab_drive.py /path/to/Vettalume_Question_Bank_by_Exam.xlsx --exam CAT --steps 30 --accuracy 0.85

# include drafts too (drive over the WHOLE bank while authoring)
python scripts/mab_drive.py bank.xlsx --exam CAT --steps 60 --accuracy 0.75 --include-drafts

# always-right / always-wrong, different RNG seeds
python scripts/mab_drive.py bank.xlsx --exam GMAT --accuracy 1.0
python scripts/mab_drive.py bank.xlsx --exam CAT  --accuracy 0.5 --seed 3
```

### 2. Upload + poke the API — `/docs`

Boot the app (Option B above) and open **`/docs`**. Use **`POST /ingest/question-bank/xlsx`** to
upload the workbook, then `POST /auth/dev-login`, set the returned id as the `X-Learner-Id` header,
and call `GET /learn/next` → `POST /learn/answer` → `GET /learn/map` to step the loop by hand.

### 3. Interactive console — `/console`

Open **`/console`**. It wears the dashboard skin but is driven 100% by the real API: a Learn pane
(`/learn/next` loop with live P/D/M bars + MAPLE edge ladder), a Map pane (`/learn/map` with
locks/recommendation), and an event log of every call. Best for *seeing* the engine behave.

* **Skip** (`skip →` button): drops the current question for this session and serves a different one,
  so you're never stuck on a single item. Skipped items don't come back until you reset the learner.
  Under the hood it's `GET /learn/next?exclude=<comma-separated item ids>`; the engine walks to the
  next concept when a concept's items are all excluded.
* **Visible ZPD**: every concept in the map shows its prerequisites with *your current mastery* of
  each and the H threshold (e.g. `🔒 blocked — needs Core exponent laws 42% / need 74%`). The map
  response carries `mastery_threshold` and per-node `prereqs_detail` (`{name, mastery, met}`).

### Two runtime toggles (env vars)

| Env var | Default | Effect when flipped |
|---|---|---|
| `SERVE_ONLY_APPROVED` | `true` | `false` → serve **draft** items too (drive the MAB over the whole bank during authoring) |
| `ZPD_USE_PREREQS` | `true` | `false` → ZPD **ignores prerequisites** — nothing locks; the topic bandit picks purely on room-to-grow |

Set them on the run command (or in `.env`):

```bash
SERVE_ONLY_APPROVED=false ZPD_USE_PREREQS=false uvicorn app.main:app --reload --port 8001
```

> **`Status` gates what's served.** Only `approved` questions reach a learner; `draft` (and anything
> else) are held. The bandit will **not** recommend a topic/concept whose only items are drafts — so
> if a drive ends quickly, check your approved count (`--include-drafts` or `SERVE_ONLY_APPROVED=false`
> to drive over everything, or mark more rows `approved`).

---

## No-repeat selection + difficulty-ladder mastery (v0.2.9)

Two coupled selection bugs were fixed so the MAB feels right while solving:

**1. Questions no longer repeat.** The problem bandit now *hard-prefers fresh (never-answered) items*:
while any unseen item remains in a concept it will never re-serve one you've already answered. Only
once the entire fresh pool is exhausted does `next_step` fall back to **spaced review** (mode
`"review"`, least-recently-seen first) — so a repeat only ever appears as deliberate, labelled
review, never as the same question moments later.

**2. Difficulty actually climbs.** Previously one lucky correct answer "mastered" a concept, so you
saw a single mid-difficulty item per concept and never reached the hard ones. Now:
* a concept stays in rotation until it is *mastered* — `mastery ≥ H` **and** enough attempts to have
  worked up its ladder (`MASTERY_MIN_ATTEMPTS = 3`, capped at the concept's item count so a 1- or
  2-item concept is still masterable);
* the **topic** bandit likewise keeps a topic in play until its concepts are mastered, not merely
  above H;
* as you answer, the MAPLE edge moves (right → harder, wrong → easier) and the next fresh item tracks
  it, so a strong learner climbs d0 → d1 → d2 within each concept.

Prerequisite **locks are unchanged** — they still release at `mastery ≥ H` (one strong answer), so
ZPD unlocking stays fast; only *advancement through a concept's own items* now requires the ladder.

Verified against the real 42-question bank by driving the exact console flow at three skill levels:

| Learner (accuracy) | Distinct served | Fresh repeats | Difficulty spread |
|---|---|---|---|
| Strong (0.9) | 36 | **0** | −1, 0, 1, **2** |
| Mixed (0.6) | 31 | **0** | −1, 0, 1, **2** |
| Struggling (0.35) | 18 | **0** | −1, 0, 1, **2** |

Practise in `/console` (or click **↻ simulate practice** on `/chapter`): every question is different
until you've seen them all, and difficulty adapts to how you're doing.

---


## Chapter analytics — `/chapter` + `/analysis/*`

Once a learner has practised a chapter (a **topic**, e.g. *Indices and Surds*) and worked its
subtopics (the **concepts** under it), the platform exposes a full per-chapter analysis. Everything
is derived live from the append-only response spine + node states — no new tables.

**The page:** open **`/chapter?exam=CAT&topic=Indices and Surds`** (or just `/chapter` and pick from
the dropdown). It reuses the chapter design and fills every section from the API. A **↻ simulate
practice** button auto-answers ~60 questions so you can see it populated without hand-solving, and the
exam segmented control + chapter dropdown let you switch context.

**The endpoint:** `GET /analysis/chapter?exam=&topic=` (or `&topic_id=`) returns the eight blocks the
dashboard renders, all scoped to the current learner (`X-Learner-Id`):

| Block | What it is | Derived from |
|---|---|---|
| `kpis` | questions answered, topic mastery, concepts learnt / total, overall accuracy | spine count + node states |
| `difficulty_spread` | accuracy per band **D1..D5** (= authored difficulty −2..2) | responses grouped by `difficulty_d` |
| `improvement_over_time` | cumulative-correctness curve; **weekly** if activity spans ≥ 2 weeks, else a within-session **progress** curve (`unit` says which) | response timestamps |
| `learning_vs_practice` | time split — first attempt on a concept counts as *learning*, repeats as *practice* (`Hh Mm` + %) | `response_time_ms` per concept |
| `strongest` / `weakest` | top / bottom concepts by mastery | node states |
| `recommended_actions` | *Learn X next* / *Revisit Y* — same MAB the learn loop uses (`within_topic_candidates`) | bandit |
| `practice_test` | last scored mock batch in this chapter (0 until mocks exist) | spine (mock contexts) |
| `subtopics` | every concept: mastery, learned, mastered, attempts, accuracy, edge | node states + spine |

**Populate it for testing** (dev only — needs `dev_mode`, on by default):

```bash
# auto-drive the MAB for the current learner and record real responses
curl -s -X POST "http://localhost:8001/analysis/simulate?exam=CAT&steps=60&accuracy=0.7" \
  -H "X-Learner-Id: <your-learner-id>"

# then read the analysis
curl -s "http://localhost:8001/analysis/chapter?exam=CAT&topic=Indices%20and%20Surds" \
  -H "X-Learner-Id: <your-learner-id>" | python3 -m json.tool
```

Or simplest: open `/chapter`, click **↻ simulate practice**, watch it fill. Lower `accuracy` →
more wrong answers → more *revise* activity → richer practice-time + a more varied curve. The page
and `/console` share the same browser learner (`localStorage.vl_learner`), so practising in the
console then opening `/chapter` shows your real numbers.

---

- **Catalog:** `Exam`, `Section`.
- **Knowledge graph:** `KnowledgeNode` (tree via `parent_id`) + `PrereqEdge` (the DAG).
- **Shared item bank:** `Item` (authored fields + derived calibration fields kept separate).
- **Event spine:** `Response` — append-only, **context-aware** (`practice` / `diagnostic` /
  `sectional_mock` / `full_mock`). Only cold contexts are admissible for calibration.
- **Exposure ledger:** `Exposure` — drives the shared-bank eligibility rule (mocks strictly
  exclude seen items; practice allows repeats).
- **Mastery store:** `LearnerNodeState` (Phase-0 placeholder mastery; replaced in Phase 1).
- **Ingestion QC gate:** validate-all-then-commit, controlled-vocabulary FK checks, idempotent
  dedup, content-change versioning that invalidates calibration.
- **Auth:** Phase-0 stub (learner_id = bearer token).

## Learning engine (Phase 1)

The adaptive Learning loop — a hierarchical multi-armed bandit over the knowledge graph. Runs on
observed correctness + expert difficulty (1–5); **no IRT, no calibration**. All state is derived
from the spine; `LearnerNodeState` is a cache.

- **Mastery** (`engine.py`) = `0.40·P + 0.30·D + 0.30·M` (eq8): `P` = EWMA accuracy (β=0.8),
  `D` = difficulty-weighted accuracy, `M` = decaying memory traces with a spacing effect.
  Threshold `H = 0.74`.
- **Topic bandit + ZPD:** a topic unlocks only when every prerequisite topic is mastered; among
  unlocked, unmastered topics the engine picks the highest expected learning gain (room-to-grow ×
  momentum).
- **Problem bandit + MAPLE:** within a concept, question difficulty tracks a MAPLE "edge" (starts
  3, ±0.4 per answer, clamped 1–5); items are chosen by a Gaussian difficulty prior around the edge.
- **Memory Chain review:** mastered concepts whose memory trace decays below threshold surface for
  spaced review.

Endpoints (all need the `X-Learner-Id` header):

| method | path | purpose |
|---|---|---|
| GET | `/learn/next?exam=CAT[&section=QA]` | engine's next decision: topic → learn/revise → question (+ teaching content on a new concept) |
| POST | `/learn/answer` | record an answer; returns correctness, P/D/M breakdown, mastery, edge, review status |
| GET | `/learn/map?exam=CAT[&section=QA]` | full map: topics with lock/mastery/recommended + per-concept state |
| GET | `/learn/reviews?exam=CAT` | concepts due for spaced review |
| GET | `/learn/concept/{node_id}` | per-concept analytics |

Two ways to test it:

- **Visual playground** (clickable): open **`http://localhost:8001/play`** — answer questions, watch
  mastery climb and topics unlock. A test client for the backend; your real frontend will call the
  same endpoints.
- **Simulation** (scripted): `python scripts/learning_sim.py` — prints a learner mastering
  Averages → Ratio → Mixtures (unlocking), with the P/D/M breakdown and MAPLE edge each step.

**Tuning:** all engine constants live at the top of `app/services/engine.py`. Note
`MASTERY_MIN_ATTEMPTS` (default `3` as of v0.2.9 — a concept must be worked up its difficulty ladder,
not cleared by one lucky correct; it is capped per concept at the number of items available so small
concepts stay masterable). Prerequisite *unlocking* still releases at `mastery >= H`.

---

## Psychometric IRT — scoring + calibration (Phase 2)

Phase 2 adds the mock-side psychometric engine. It is kept **strictly separate** from the Learning
side: Learning uses a blended 0–1 mastery (no IRT); **mocks** use an IRT ability `theta` on the
−3..+3 scale. Calibration reads **cold responses only** (`diagnostic`, `sectional_mock`, `full_mock`)
— practice answers are contaminated by teaching and never feed it. Authored difficulty is only a
*prior* for `b`; it never authors `a` or `c`.

**The 3PL model.** `P(correct) = c + (1 − c) / (1 + e^(−a(theta − b)))` — `b` difficulty, `a`
discrimination, `c` the guessing floor (≈ 1/options for MCQ, ≈ 0 for type-in).

**Ability (`app/services/irt.py`, `ability.py`).** Estimated by **EAP** — the mean of the posterior
belief curve over a `theta` grid (−3..+3 step 0.5) starting from a `N(0,1)` prior, so the estimate is
finite and sensible from the very first answer. Uncertainty is `SE = 1/sqrt(total information)`. A
cheap **Elo** running update (`theta += K(R − P)`) is also available. The reference's worked numbers
are pinned as tests: one correct on a `b=0` item → `theta ≈ +0.275 (SE 2.42)`, one wrong → `−0.41`,
the climb `+0.55 → +0.81 → +0.57`.

**Calibration worker (`app/services/calibration.py`).** Discovers `(a, b, c)` from the cold
right/wrong matrix:
1. **bootstrap** — ability ≈ `logit(score)`, `b ≈ −logit(item p-correct)`, `a=1`, `c=floor`;
2. **Step A** — re-estimate every learner's `theta` by EAP (items fixed), then recenter abilities to
   mean 0 / sd 1 to anchor the scale;
3. **Step B** — re-fit every item's curve by regularized (MAP) Newton-Raphson (abilities fixed);
4. loop A/B until parameters stop moving.

It is **phased**, because thin data cannot support all three parameters (the reference shows `b`
recovers at ~40 responses, `a` is weak, `c` is noise):

| Responses per item | Estimates | Fixes |
|---|---|---|
| `< two_pl_at` (default 500) | `b` only | `a=1`, `c=floor` |
| `< three_pl_at` (default 2000) | `b`, `a` (2PL) | `c=floor` |
| `>= three_pl_at` | `b`, `a`, `c` (3PL) | — |

A strong prior keeps `c` near the format floor (freely fitting it diverges and corrupts `a`/`b`), and
each Newton step is clamped — so the 3PL phase is stable rather than blowing up.

**Versioned parameter store.** Every run writes a `CalibrationRun` and one `IrtParameter` row per item
(`a, b, c, phase, n_responses`, run lineage). The row with `active=True` is live; recalibration flips
the flag, retaining history for rollback and drift checks. The active set is mirrored onto
`Item.irt_*` with a version bump. `active_params()` falls back to the authored prior when an item has
never been calibrated.

**Simulation harness = the release gate (`app/services/sim_harness.py`).** Manufactures a population
with **known** true `a, b, c`, answers the bank through the 3PL, runs the exact production calibration
loop, and reports recovery (correlations + RMSE). Calibration is only trusted once it recovers
difficulty on synthetic data. Verified recovery (60 items):

| Responses/item | Phase | `b` corr | `a` corr | `c` corr |
|---|---|---|---|---|
| ~40 | b-only | ~0.87 | — (fixed) | — (fixed) |
| ~600 | 2PL | ~0.98 | ~0.84 | — (fixed) |
| ~2200 | 3PL | ~0.91 | ~0.55 | ~0.66 |

`b` is recovered well throughout; `a` needs the 2PL volume; `c` is marginal even at the 3PL volume —
exactly the reference's honest finding.

### Endpoints (`/irt`, dev where noted)

| Method · path | What |
|---|---|
| `POST /irt/simulate?students=&items=&seed=&b_min=` | **dev** — run the gate; returns recovery + pass/fail |
| `POST /irt/calibrate?exam=&two_pl_at=&three_pl_at=&activate=` | **dev** — calibrate cold responses, write a versioned set |
| `GET /irt/runs?exam=` · `GET /irt/runs/{id}` | calibration history; run detail with per-item params |
| `GET /irt/item/{item_id}` | active `(a,b,c)` + source (`calibrated`/`authored_prior`) + full history |
| `POST /irt/score?exam=&scope=&session_id=&method=` | score the current learner's cold/mock answers → `theta` ± SE |
| `GET /irt/ability?exam=` | the learner's latest stored `theta` |

**Try it.** The gate needs no data: `POST /irt/simulate` → see `b_corr` and `gate.passed`. To
calibrate real data you first need cold responses (diagnostic/mock context); with the current sparse
bank everything stays in the honest **b-only** phase. `c` only unlocks around ~2000 responses/item.

### Run an IRT mock and watch theta move (`scripts/irt_mock.py`)

An interactive adaptive mock that prints the ability estimate after every answer — the quickest way to
see IRT working. It serves the highest-information question at your current ability (a real CAT move),
records each answer in the cold `full_mock` context, and re-estimates `theta` by EAP (−3..+3) with its
SE and 95% interval. No server needed; each run is a fresh session from `theta = 0`.

```
python scripts/irt_mock.py PATH_TO_BANK.xlsx --exam GMAT
python scripts/irt_mock.py PATH_TO_BANK.xlsx --exam GMAT --section Quant --max 20
```

If no path is given it looks for `~/Downloads/Vettalume_Question_Bank_by_CAT_GMAT.xlsx`. Items are
uncalibrated, so `b` is the authored difficulty, `c` the option floor, `a = 1` — exactly how a
cold-start diagnostic scores before calibration. Answer correctly and `theta` climbs while the engine
serves harder questions; struggle and it falls and serves easier ones, with SE shrinking as evidence
accumulates.

## Mocks — delivery engines + scoring adapters (Phase 3)

Phase 3 turns the IRT core into an actual mock product: three delivery engines and three scoring
adapters on one psychometric core, with exposure control and a per-response durability checkpoint —
the split is straight from the PRD (one engine and one adapter per exam).

**Delivery engines (`app/services/mock_delivery.py`).** A shared next-item interface, three engines:
* `item_adaptive` (GMAT) — continuous max-information selection at the current `theta`, exposure
  applied first;
* `mst` (GRE) — a fixed routing module, then route by interim `theta` into an easy / medium / hard
  panel assembled by difficulty band;
* `fixed_form` (CAT) — a pre-assembled balanced linear form, served in order, no adaptivity.

All three read the active calibrated `(a,b,c)` (falling back to the authored prior) and never re-serve
within a session or across a learner's earlier mocks.

**Exposure control.** `exposure_counts()` tracks how often each item has been served across the whole
population; selection applies an optional exposure cap and then randomesque-picks among the top-K most
informative items weighted toward the less-exposed — the IRT doc's "exposure limits before maximum
information". `GET /mock/exposure/report` surfaces the most-served items.

**Scoring adapters (`app/services/mock_scoring.py`).** The core emits a `theta`+SE (overall and
per-section); each adapter maps it to its exam's scale:
* `composite` (GMAT) — 205–805 total + 60–90 section scores;
* `sectional` (GRE) — per-section 130–170 + the panel taken (essay 0–6 scored separately);
* `percentile_call` (CAT) — `theta` → percentile (normal-CDF norm) → per-institute **call
  probability** = `P(true percentile ≥ cutoff)` from `theta ± SE`, with a confident-yes / confident-no
  / too-close-to-call read off the SE band.

**Per-response reliability checkpoint (`MockSession` + `app/services/mock_session.py`).** The
`MockSession` row is committed after **every** answer with the running `theta`, SE, marginal
reliability (`rho = 1 − SE²`), delivery cursor, and served list — so a dropped connection loses
nothing; `GET /mock/{sid}` resumes from exactly that state. Item-adaptive mocks stop on the SE target
(`se_target`, default 0.30 — "precise enough") or `max_items`. Mock responses are recorded in a cold
mock context (feeding IRT/calibration) and **never touch the Learning-side 0..1 mastery** — that
separation is now enforced in `concept_attempts` (practice-context only).

### Endpoints (`/mock`)

| Method · path | What |
|---|---|
| `POST /mock/start?exam=&mode=&section=&max_items=&se_target=` | begin a mock (`mode` = item_adaptive / mst / fixed_form); returns the first question |
| `GET /mock/{sid}/next` | next question per the engine (or the final score when complete) |
| `POST /mock/{sid}/answer` `{item_id, answer_given}` | grade, recompute `theta`/SE/reliability, advance, checkpoint |
| `GET /mock/{sid}` | resume — the checkpointed state after a dropped connection |
| `GET /mock/{sid}/score` | the exam-native score (composite / sectional / percentile+call) |
| `GET /mock/exposure/report?exam=` | population exposure (most-served items) |

Each `mode` works on any exam with items spread across difficulty; with uncalibrated items the engine
uses the authored difficulty as `b`, so you can run a full mock today and recalibrate later.

## Diagnosis chassis + plan engine (Phase 4)

Phase 4 is where the platform stops *measuring* and starts *prescribing*. It classifies every miss by
**cause** (not aggregate accuracy), ranks the learner's leaks, and turns that into a prerequisite-ordered
study plan that re-plans continuously and explains each change in plain language.

Two chassis rules from the platform PRD are enforced in code:
- **Diagnose before prescribing.** With no practice signal the diagnosis returns `insufficient_data`
  and the plan engine *refuses* to plan (`status: "refused"`).
- **Gap detection in Part 1, not Part 2.** The primary diagnosis reads the **practice loop only**
  (`context == practice`). A mock is decomposed by the *same* taxonomy as a separate rendering
  (`/diagnosis/mock/{sid}`), never folded into the practice diagnosis. (This keeps the three estimators
  — 0..1 learning mastery, IRT theta, and diagnosis — strictly separate, as in Phases 1–3.)

### The cause taxonomy (4 shared + 2 exam-native)

| Cause | Bucket | Fires when |
|---|---|---|
| `concept_gap` | foundations | needed a hint and still missed, a prerequisite is unmet, or concept mastery is below threshold |
| `process_error` | execution | concept is held, normal time — the rule is held but the procedure broke |
| `timing_pressure` | execution | concept is held, response time over the slow threshold (accuracy collapses under pace) |
| `careless_slip` | execution | concept is held, very fast answer on an easy item (capability present, lapse) |
| `vocabulary_gap` | vocabulary | **GRE** only, on a vocabulary item — remediated by the vocab engine, not reasoning practice |
| `selection_error` | selection | **CAT** only, a snap attempt on a hard item under negative marking (should have been skipped) |

Each node carries a **cause mixture** (the fraction of its misses by cause) and a **dominant cause**.
The headline **strategy decomposition** rolls all misses into foundations / execution / selection /
vocabulary — *decompose strategy instead of reporting aggregates*.

### Leak ranking (recency-weighted)

A leak's score sums its misses, weighted by difficulty and by **recency** (`RECENCY_DECAY` per newer
attempt on the same concept). A node whose recency-weighted score falls below `LEAK_FLOOR` is treated
as **resolved** and drops out of the diagnosis and the plan — so *candidates do not repeat what is
broken*. Closing a concept (recent correct work) removes it on the next re-plan.

### Plan engine

For each top leak the plan schedules any **unmet prerequisite first** (you cannot fix Mixtures while
Ratios is broken), then the leak itself with a **cause-appropriate remediation** (concept gap ->
learn the concept; process -> drill the method; timing -> timed sets; careless -> accuracy reps;
vocabulary -> vocab engine; selection -> selection trainer). Plans are **versioned**: each re-plan
diffs against the prior active version (added / removed / reprioritized) and emits a plain-language
explanation. Ability is published as a **range** (`band_95`), never a bare point — the platform-wide
honesty contract.

### Endpoints (`/diagnosis`, `/plan`)

| Method + path | Does |
|---|---|
| `GET /diagnosis?exam=` | full diagnosis: cause mixture per node, leak ranking, strategy decomposition, honest ability range |
| `GET /diagnosis/leaks?exam=&top=` | the ranked leaks (+ decomposition) |
| `GET /diagnosis/mock/{sid}` | decompose one mock's misses by the same taxonomy (a rendering, kept separate) |
| `POST /plan/generate?exam=` | generate / re-generate the plan; refuses without signal; returns diff + explanation on a re-plan |
| `GET /plan?exam=` | the current active plan |
| `GET /plan/history?exam=` | every plan version with its change explanation |

### Honest limitations (heuristic layer)

The cause **thresholds** (`SLOW_MS`, `FAST_MS`, `EASY_D`) and the leak **decay/floor** are deliberate,
explicit heuristics — tune them on real data. `selection_error` and `vocabulary_gap` currently use
proxies (a fast attempt on a hard CAT item; a "vocab" tag on the node/section/format); they become
precise once the delivery layer captures real skip/attempt decisions and the GRE bank carries vocab
tags. The diagnosis is only as good as the practice signal feeding it.

## Analysis/debrief, review surfaces, billing, Honest Perimeter (Phase 5)

Phase 5 adds the four shared layers that sit above the engine: a mock **debrief**, **review surfaces**,
a **billing/entitlements** system, and the platform-wide **Honest Perimeter** that every prediction
must pass through.

### The Honest Perimeter (enforced, not aspirational)

Three rules live in `services/honesty.py`:
- **Ranges, not points.** Every prediction is wrapped in a 95% band (`perimeter` from a point+SE, or
  `perimeter_band` from an explicit band when the SE is not in the reported units).
- **No inflation.** A prediction with too little supporting evidence (`basis_n < MIN_BASIS_N`) is
  marked `is_claim: false` / `basis: "provisional"` — shown as an estimate to be confirmed, never as
  a claim. A 3-item mock score comes back provisional on purpose.
- **A published accuracy record.** Each emitted prediction is logged (`PredictionRecord`); once a real
  outcome is known, the band's coverage and point error are computed. `GET /honesty/accuracy` reports
  band coverage (should sit near 0.95 if the bands are honest) and mean error, and stays *unpublished*
  until enough outcomes accumulate (`PUBLISH_MIN_N`).

### Debrief + review surfaces (`/review`, `/honesty`)

| Method + path | Does |
|---|---|
| `GET /review/mock/{sid}` | full mock debrief: honest score **band**, cause decomposition (Phase-4 taxonomy), timing read, section breakdown; the item-by-item review with solutions is a **paid** surface, the free tier gets the score + counts |
| `GET /review/queue?exam=` | shared review surface: items the most recent attempt got wrong, grouped by concept with solutions, plus due spaced reviews |
| `GET /review/progress?exam=` | honest ability **trend** (each point banded), topic mastery, open leaks, and the published accuracy record |
| `GET /honesty/accuracy?exam=&kind=` | the platform's published prediction-accuracy record |

The debrief logs its headline as a `PredictionRecord`, so the accuracy record builds itself as mocks
are scored. The free-vs-paid split is driven by the learner's entitlement and only bites when
enforcement is on.

### Billing / entitlements (`/billing`)

One account, one wallet, independently-purchasable per-course entitlements, a multi-exam bundle with a
cross-course discount, and per-course free tiers (BL-01..05, AC-01). Tier is encoded in
`Entitlement.status` (`free` | `active` | `expired`); the SKU catalog, orders, and prediction records
are the only new tables.

| Method + path | Does |
|---|---|
| `GET /billing/catalog` | the SKU catalog: per-course free + paid tiers (USD for GMAT/GRE, INR for CAT) and the GMAT+GRE bundle |
| `GET /billing/entitlements` | the learner's entitlements |
| `GET /billing/access?exam=` | entitlement state + free-tier usage + whether enforcement is on |
| `POST /billing/grant-free` | grant the per-course free tier |
| `POST /billing/purchase` | record a purchase and grant entitlement(s) — bundles grant several at once and report the saving |

**`purchase()` records an order and grants entitlements; it does NOT move money.** A real payment
provider (Stripe for USD, Razorpay for INR) integrates at that one function. This is the
records-and-logic layer the PRD specifies, not a payment integration.

### Enforcement is a toggle

`enforce_entitlements` (env `ENFORCE_ENTITLEMENTS`, default **false**) keeps the demo fully open. When
switched on, `billing.enforce(...)` gates course-scoped surfaces: a paid surface requires an active
entitlement (HTTP 402 otherwise), the free tier is auto-granted for baseline access, and a metered
free-tier resource (e.g. number of full mocks) blocks with 402 once exhausted. The machinery is real
and tested; turning it on is one flag, so no existing flow breaks until you choose.

### Honest limitations

The score-space band is exact for GMAT (total) and CAT (percentile); GRE currently publishes the
native ability range with a note that the panel-aware score band is pending. The accuracy record only
becomes meaningful once real outcomes are fed back via `honesty.record_outcome(...)`. Free-tier limits
beyond `full_mocks` are catalog-data, not yet wired into every surface. No real payment provider is
connected — `purchase()` is the integration seam.

## Real auth, cross-exam warm-start, GMAT/GRE mounted (Phase 6)

Phase 6 puts a real identity layer under the platform, turns the "one account, signals transfer"
thesis into working code, and brings GMAT and GRE online as full course lines.

### Real JWT auth (`/auth`)

Password accounts and HS256 JWTs, **stdlib only** (no new dependencies). Passwords are PBKDF2-HMAC-
SHA256 with a per-user salt; tokens are signed JWTs with the algorithm pinned (alg=none and RS/HS
confusion are rejected) and verified in constant time. All of it lives in `services/security.py`, so
swapping to argon2 + PyJWT for production is a one-file change.

| Method + path | Does |
|---|---|
| `POST /auth/register` | create an account with a password, return a Bearer JWT |
| `POST /auth/login` | verify email + password, return a Bearer JWT (one error message -> no account enumeration) |
| `GET /auth/me` | the authenticated learner |
| `POST /auth/dev-login` | dev convenience (no password), returns a JWT + `learner_id`; disabled when `dev_mode` is off |

`get_current_learner` accepts a **Bearer JWT** first and falls back to the legacy `X-Learner-Id` header
for dev, so every existing flow keeps working. Set `REQUIRE_JWT=true` to disable the legacy header and
make JWT mandatory. **`JWT_SECRET` must be overridden in production** (the default is a dev placeholder).

### Cross-exam warm-start (`/account/warm-start`, AC-02)

A learner's second course starts warmer than cold. Three shared constructs transfer across courses —
**quant core**, **Reading Comprehension**, and the **data-logic engine** (GMAT Data Insights <-> CAT
DILR). When a course is newly entitled, ability measured on those constructs in the learner's other
courses seeds a prior for the new one.

The honesty rule is enforced: a transferred prior is **always provisional** (`is_claim: false`) and its
SE is **inflated** for crossing exams — shown as an estimate to be confirmed, never as established
ability (the Honest Perimeter from Phase 5 does the marking). Warm-start runs automatically when an
entitlement is granted (free or paid), and `GET /account/warm-start?exam=` previews the transfer
without persisting.

### GMAT and GRE mounted

`services/mount.py` seeds the GMAT and GRE exam structures — sections with construct-mappable keys
(QA -> quant, VA -> rc, DI -> data_logic), concepts, and approved items — on boot, idempotently. All
three courses are now live for mocks, diagnosis, billing, and warm-start. (The items are scaffold
samples so the engine runs end-to-end; real GMAT/GRE content is authored per the exam PRDs.)

### Honest limitations

The transfer uses a fixed cross-exam variance penalty (`TRANSFER_VAR`) rather than an empirically
fitted one, and pools cold responses by construct rather than running a full multidimensional model.
The mounted GMAT/GRE items are scaffolds, not authored content. The hand-rolled JWT is correct for
HS256 but production should use a vetted library; the dev `JWT_SECRET` must be replaced.

## Production serving & scale (Phase 8)

The app is built to scale horizontally; this phase makes that real and proves it on actual Postgres.
Two things landed, plus one important bug fix.

**Runs on real Postgres, with a real connection pool.** `DATABASE_URL` swaps the engine; on Postgres
the app now uses a bounded `QueuePool` (`db_pool_size`, `db_max_overflow`, `db_pool_timeout`,
`db_pool_recycle`, all env-tunable). Verified end-to-end against Postgres 16: DDL (`create_all`), JSON
columns, UUID keys, foreign-key enforcement, the full learning write path, and the mock engine.

**Bug fix found only because we tested on Postgres.** The CAT seed inserted `items` before the parent
`knowledge_nodes` they reference. SQLite doesn't enforce foreign keys by default, so it passed silently
— and would have crashed the first real Postgres boot. Fixed the seed ordering, and **turned on SQLite
FK enforcement** (`PRAGMA foreign_keys=ON`) so this class of bug now fails loudly in the test suite
instead of in production. All 97 tests pass with FK enforcement on.

**Multi-worker serving behind a load balancer.** Production runs gunicorn supervising uvicorn workers
(`gunicorn_conf.py`) — every CPU core used, crash-resilient (workers respawn), workers recycled
periodically against slow leaks. Because the app is stateless (identity is in the JWT), you also run
many *containers* behind nginx (`docker-compose.prod.yml` + `nginx.conf`) and scale across machines.
Proven: gunicorn with 3 workers on Postgres served 50 concurrent requests cleanly.

```bash
# local: multi-worker against your Postgres
WEB_CONCURRENCY=4 DATABASE_URL=postgresql+psycopg2://… gunicorn -c gunicorn_conf.py app.main:app

# production: full stack (Postgres + Redis + N app workers + nginx LB)
export JWT_SECRET="$(openssl rand -hex 32)"
export ADMIN_EMAILS="you@yourco.com"
docker compose -f docker-compose.prod.yml up -d --build --scale app=3
# scale the app tier any time; nginx picks up replicas automatically:
docker compose -f docker-compose.prod.yml up -d --scale app=6
```

**The connection budget (the operational rule that keeps it up).** Each worker holds up to
`db_pool_size + db_max_overflow` (default 5 + 10 = 15) Postgres connections. Total at peak ≈
`app_replicas × WEB_CONCURRENCY × 15`. Keep that under Postgres `max_connections` (the prod compose
sets it to 200). Example: 3 replicas × 4 workers × 15 = 180 < 200 ✓. To scale wider than that, add
**pgbouncer** to multiplex (next on the path) rather than just raising `max_connections` forever.

**What this gets you, and what's still ahead.** This is steps 1–2 of the scale path (Postgres +
horizontal serving) — enough to handle everyday concurrent load by adding replicas. The mock-day spike
(tens of thousands starting/submitting at once) still needs the remaining items: move calibration to an
**offline worker** (Redis is wired in compose), **vectorise IRT with numpy**, **Redis-cache** hot
reads, **pgbouncer**, and **async mock-submit scoring**. Then load-test the spike for a real ceiling.

## Admin content portal (Phase 7)

Content authoring now sits behind an admin perimeter — "no outsiders." Before this phase the
`/ingest/*` endpoints had **no auth at all** (anyone who could reach the server could post questions),
and the syllabus graph could only be created in code. Phase 7 closes both gaps.

**The perimeter.** An account is an admin iff its email is in `ADMIN_EMAILS` (env) **or** it has a row
in the additive `admin_users` table. `require_admin` guards every content route: unauthenticated → `401`,
authenticated non-admin → `403`. It accepts **only** a real Bearer JWT (never the legacy passwordless
`X-Learner-Id`), so knowing an admin's account id can't be used to impersonate them. The same guard now
also protects the pre-existing `/ingest/*` upload endpoints.

**Secure bootstrap (no public "make me admin" hole).** Mint the first admin on the server:

```bash
python -m scripts.create_admin you@yourco.com "a-strong-password" "Your Name"
```

It creates/updates the account + password and grants the admin role. After that, that admin can grant
others from the portal (Admins tab) or `POST /admin/admins`. On boot, `ensure_admins()` also promotes any
already-existing accounts whose email is in `ADMIN_EMAILS`.

**The portal** is served at `GET /admin` (`static/admin.html`). It's a login page; all data and actions
behind it require an admin JWT, so opening the URL does nothing without credentials. Four areas:
- **Syllabus** — view/build the graph: exams, sections, topics, concepts, prerequisite edges; delete nodes
  (guarded: a node with child concepts or items can't be deleted until emptied).
- **Questions** — list items (with answers), filter by exam/concept/status, create a single question,
  edit (version bumps), approve, retire, delete.
- **Bulk upload** — drag-drop the question-bank `.xlsx` (same format as the authors' path) → import report.
- **Admins** — list / grant / revoke.

**API (all under `/admin`, all admin-gated).** `GET /admin/me`, `GET /admin/exams`,
`GET /admin/syllabus?exam=`; `POST /admin/exams|sections|topics|concepts|prereqs`,
`DELETE /admin/prereqs`, `DELETE /admin/nodes/{id}`; `GET /admin/items`, `POST /admin/items`,
`PATCH /admin/items/{id}`, `POST /admin/items/{id}/approve|retire`, `DELETE /admin/items/{id}`,
`POST /admin/items/upload-xlsx`; `GET /admin/admins`, `POST /admin/admins`, `DELETE /admin/admins/{id}`.

The single-item create runs through the **same validated ingest path** as bulk upload, so the
authored-vs-derived boundary holds (you still can't author IRT `a`/`b`/`c`). Bulk xlsx remains the
workhorse for volume; the portal is for the graph, approval/QC, and spot-edits. A newly authored +
approved question is immediately served by `/practice/next` — the upload-then-serve loop is closed.

The full five-pass QC review workflow from the PRD is a later phase; today the lifecycle is
draft → approved → retired with `SERVE_ONLY_APPROVED` gating what learners see.

## Roadmap

| Phase | What | Status |
|---|---|---|
| 0 | Walking skeleton + schema + ingestion | **done** |
| 1 | Learning engine: ZPD + concept/problem MAB + MAPLE + blended mastery + MCM | **done** |
| 2 | Psychometric IRT core (EAP + Elo) + calibration worker + sim harness + versioned store | **done** |
| 3 | Mocks: 3 delivery engines + 3 scoring adapters + exposure control + per-response checkpoint | **done** |
| 4 | Diagnosis chassis: cause taxonomy + cause mixture + leak ranking; plan engine: prereq-ordered re-plan + plain-language diffs | **done** |
| 5 | Analysis/debrief + review surfaces + billing/entitlements (multi-currency, bundles, free tiers) + Honest-Perimeter enforcement | **done** |
| 6 | Real JWT auth (stdlib) + cross-exam warm-start (provisional priors) + GMAT/GRE mounted | **done** |
| 7 | Admin content portal: admin role + guard (content perimeter), syllabus CRUD, item lifecycle (edit/approve/retire), locked-down bulk upload, admin management | **done** |
| 8 | Production serving & scale (part 1): real Postgres + bounded connection pool, SQLite FK-parity, multi-worker gunicorn + nginx load balancer (horizontal scaling) | **done** |
| 8b | Scale (part 2): offline calibration worker, numpy IRT, Redis caching, pgbouncer, async mock-submit scoring, spike load-test | next |
| 9 | Payment-provider integration + course switcher (AC-04) + exam-native surfaces (GRE vocab/essay, CAT IIM dashboard) | later |

## Notes / deliberate Phase-0 simplifications
- `create_all` instead of Alembic migrations (Alembic arrives when the schema stabilises).
- `Item` is one-row-per-id with version-bump-on-change; immutable versioned rows arrive in
  Phase 2 when calibration makes version binding load-bearing.
- Enum-like fields stored as strings for portability; can become native PG enums later.
- Auth is a stub; real auth is Phase 5.

## Phase 9 — Content CMS + student management (v0.10.0)

The admin portal (`/admin`) gained a full content-management surface that mirrors the learning structure, plus student administration.

**Learning content (per subtopic).** Under the new **Learning content** tab an admin drills down exam → section → chapter (topic) → subtopic (concept). At the subtopic level they can:
- write a **concept explanation** and attach **video links** (YouTube / Vimeo / hosted URLs with a title and optional duration). Stored on `KnowledgeNode.theory` as `{body, videos[]}` — no new table, no migration. Students read it via `GET /learn/concept/{id}` (the `content` field).
- **bulk-upload a subtopic quiz** from a simple `.xlsx` (Question / Option A–E / Answer / Difficulty / Solution). Exam, section and concept are taken from the subtopic, so the sheet only carries questions. `POST /admin/concepts/{node_id}/items/upload-xlsx?scope=both|practice_only|mock_only`.

**Mock uploads.** Mocks are assembled adaptively from the section item pool — there is no separate mock store. The Bulk-upload tab now has a **usage-scope selector**, so the same importer can tag questions `mock_only` (reserved for sectional / full mocks, never served in practice), `practice_only`, or `both`. Enforced in `state.eligible_items`.

**Student management.** A new **Students** tab: search students, open one to see their courses and answered-question count, and **deregister** them from a course or **enrol** them. Backed by `GET /admin/students`, `GET /admin/students/{id}`, `POST /admin/students/{id}/deregister`, `POST /admin/students/{id}/enroll` (entitlement-driven).

> Honest scope: video support is **links, not file hosting** (raw video files need object storage + a CDN — a separate epic). Entitlement **enforcement** is still off (`ENFORCE_ENTITLEMENTS=False`), so deregistering removes the enrolment record but does not yet block access. Unrestricted admin power over student data should grow an **audit log** and a **super-admin vs content-admin** split before production. Payments move to **Phase 10**.

Interactive engine tools: `scripts/irt_mock.py` (adaptive IRT, watch θ tighten) and `scripts/mab_play.py` (Learning loop — the bandit picks each question; watch mastery, the MAPLE edge, and concept unlocks). `scripts/mab_drive.py` drives the MAB deterministically at a set accuracy.

## Phase 10 — Coupons / discount codes (v0.11.0)

A complete discount-code system, with field names matching the admin console 1:1 (so the Coupons page maps with no translation).

**Model.** New additive table `coupons`: `code` (unique), `type` (`percentage`|`fixed`), `value`, `maxTotal`/`maxPerUser` (0 = unlimited), `minPurchase`, `maxDiscount`, `validFrom`/`validUntil` (datetime-local strings), `description`, `attempt`, `courses[]` (exam codes; [] = all), plus server-managed `used` and `status`. Money fields are integer paise; `value` is a percent when `type='percentage'`.

**Admin CRUD** (all under the admin perimeter, camelCase in/out): `GET /admin/coupons`, `POST /admin/coupons` (rejects duplicate codes, 409), `PUT /admin/coupons/{id}`, `POST /admin/coupons/{id}/toggle` (active↔inactive), `DELETE /admin/coupons/{id}`.

**Checkout validation** (public, read-only — the backend for "apply coupon at checkout"): `POST /billing/coupon/validate` with `{code, exam, amount}` → `{valid, discount, final, reason}`. Enforces active status, the validity window, total-use limit, course scope, and minimum purchase, then computes the discount (percentage capped at `maxDiscount`, or fixed), clamped to the order amount. Service: `billing.validate_coupon`.

> Not yet wired: consuming a use (incrementing `used`) and per-user limits happen at real redemption, which belongs in the purchase flow alongside the payment provider (a later phase). The admin Coupons page is fully served today.

Remaining backend gaps to close before the rest of the admin frontend connects: **student fields + verification**, **PDF/study materials** (signed-URL pattern, like video), **the mock builder + results** (fixed-form vs adaptive — a product decision), and **payments** (real Stripe/Razorpay).

## Phase 11 — Student fields + verification (v0.12.0)

Makes the admin **People** page real: the full student record the console shows, backed by the API with matching camelCase fields.

**Model.** New additive table `student_profiles` (joined to `accounts`, so no columns are added to the existing table): `phone`, `status` (active|inactive), `reg_type` (registered|trial|paid), `purchased_course`, `verified`, `progress`, `last_login`, `payment` JSON `{status, amount, method, date}`, `role`, `joined`, and a `deleted` soft-delete flag. Enrollments stay in `Entitlement` (the source of truth for which courses a student has).

**Endpoints** (under the admin perimeter; every student response is the console's exact shape — `{id, name, email, phone, exams, status, regType, purchasedCourse, verified, lastLogin, progress, payment, role, joined, isAdmin}`):
`GET /admin/students` (roster, excludes soft-deleted, `q` search), `GET /admin/students/{id}` (+ `responses` count), `POST /admin/students` (create account + profile + entitlements; duplicate email → 409), `PUT /admin/students/{id}` (edit fields, change email with uniqueness, sync enrollments), `DELETE /admin/students/{id}` (**soft delete** — hides from roster, keeps learning history and FKs intact), `POST /admin/students/{id}/verify` (toggle), `POST /admin/students/{id}/payment` (`{status, amount?, method?, autoVerify?}` — autoVerify marks the account verified on a successful payment), `PUT /admin/students/{id}/enrollments` (`{exams:[]}` set the exact course set). The per-course `enroll`/`deregister` endpoints remain.

**Real last-login.** `/auth/login` and `/auth/dev-login` now record `last_login` on the student's profile, so `lastLogin` reflects actual sign-ins rather than a seeded value.

> `progress` is currently a stored field (admin-set); deriving it automatically from the learner's real mastery across the syllabus is a later enhancement.

Remaining backend gaps: **PDF/study materials** (signed-URL pattern, like video), **the mock builder + results** (fixed-form vs adaptive — still needs a product decision), and **payments** (real Stripe/Razorpay).

## Phase 12 — Study materials / PDF (v0.13.0)

The real implementation behind the admin console's "Upload material" placeholder: actual file upload, storage, and entitlement-gated download per concept.

**Model.** New additive table `materials`: `id`, `node_id` (FK → the concept), `title`, `filename`, `content_type`, `size_bytes`, `data` (the file bytes), `created_at`. Bytes are stored in the DB so the feature is fully self-contained and portable (fine for modestly-sized study PDFs). At scale the bytes move to S3 object storage with signed URLs — only the storage backend changes; the table and endpoints stay.

**Admin endpoints** (behind the admin perimeter): `POST /admin/concepts/{node_id}/materials` (multipart `file` + optional `title`; 25 MB cap; empty → 400, too-large → 413), `GET /admin/concepts/{node_id}/materials` (metadata list — `{id, title, filename, contentType, size, sizeLabel}`), `DELETE /admin/materials/{id}`.

**Gated download** (student-facing): `GET /learn/materials/{id}/download` streams the file with the right `Content-Type`/`Content-Disposition`, **gated by the learner's entitlement** for the concept's exam. The gate is the materials paywall — enforced only when `enforce_entitlements` is on, so the open demo keeps working; it's the exact point an S3 signed-URL redirect would slot in. Verified: with enforcement on, a learner without the exam gets 403; granting the entitlement makes it 200.

Remaining backend gaps: **the mock builder + results** (fixed-form vs adaptive — still needs a product decision) and **payments** (real Stripe/Razorpay).

## Phase 13 — Mock builder, fixed-form (v0.14.0)

Admin-authored **fixed-form** mocks: every student who takes a mock sees the same paper the admin built. (Adaptive mock delivery still exists separately in the engine; this adds the fixed-form authoring the console builds.)

**Model.** New additive table `mocks`: `type` ('sectional' | 'full'), `name`, `status` ('draft' | 'published'), and type-specific scalars `negative` (sectional), `duration` / `scoring_marks` / `scoring_neg` / `instructions` (full). The whole section/question tree is stored as JSON in `sections` — each section carries its embedded questions (`{id, text, options, correct, difficulty, solution, image}`), so a mock is a self-contained paper.

**Endpoints** (admin perimeter, camelCase matching the console): `GET /admin/mocks?exam=&type=` (list), `POST /admin/mocks` (create — sectional seeds one section, full seeds all of the exam's real sections), `GET /admin/mocks/{id}`, `PUT /admin/mocks/{id}` (config: name / negative / duration / scoring / instructions), `PUT /admin/mocks/{id}/structure` (persist the whole sections + questions tree — the console mutates the mock in memory then saves the result here, which covers add/edit/move/delete/bulk-import in one call), `POST /admin/mocks/{id}/publish` (draft ↔ published), `DELETE /admin/mocks/{id}`.

> **Results/attempts are not in this phase.** The console's analytics (who attempted, scores, section-wise performance) require a *student* mock-taking flow that doesn't exist yet — the attempts shown in the frontend are seed data. Building mock delivery + scoring + an attempt ledger for fixed-form mocks is the natural next step, and it's where the existing adaptive `mock_delivery`/`mock_scoring` services would be extended.

Remaining backend gaps: **mock taking + results** (student-facing, generates the analytics), and **payments** (real Stripe/Razorpay).

