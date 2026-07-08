"""
MAB + ZPD + ability trace — drives Vettalume's REAL engine.py + irt.py (no re-implementation).

Run from the backend root:
    cd ~/Downloads/vettalume-backend
    .venv/bin/python mab_zpd_demo.py

It manufactures a 3-concept bank and a simulated learner, then prints — after every
answered question — the topic-bandit pick, the problem-bandit pick, the ZPD band,
the 0..1 mastery, and the running IRT ability (theta +/- SE).
"""
import importlib.util, sys, random
from datetime import datetime, timezone

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m
    spec.loader.exec_module(m); return m

engine = load("engine", "app/services/engine.py")
irt    = load("irt",    "app/services/irt.py")
now = datetime.now(timezone.utc); rng = random.Random(11)
SIG, H = engine.PROBLEM_SIGMA, engine.H
clamp = lambda x: max(-2.0, min(2.0, x))

CONCEPTS = ["Profit & Loss", "Quadratic Eqns", "Time & Work"]
DIFFS = [-2, -1, 0, 1, 2]
item_params = lambda d: (1.0, float(d), 0.25)   # a=1, b=d, c=1/4
TRUE_THETA = 0.6                                 # tweak to see the edge/ability track a different learner

log = {c: [] for c in CONCEPTS}; triples = []
mastery_of = lambda c: engine.blended_mastery(log[c], now)[0]
edge_of    = lambda c: engine.maple_edge(log[c])

print(f"\n  learner θ*={TRUE_THETA} | ZPD = edge ± {SIG} | mastered at H={H}\n")
for q in range(1, 15):
    val, _, c = max((engine.expected_gain(mastery_of(c), len(log[c])>0), -len(log[c]), c) for c in CONCEPTS)
    edge = edge_of(c)
    d = max(DIFFS, key=lambda d: engine.problem_weight(float(d), edge))
    a, b, cc = item_params(d)
    u = 1 if rng.random() < irt.prob_3pl(TRUE_THETA, a, b, cc) else 0
    log[c].append(engine.Attempt(correct=u, difficulty=d, ts=now)); triples.append((a, b, cc, u))
    e = edge_of(c); mu = mastery_of(c); th, se = irt.eap_ability(triples)
    print(f"  Q{q:>2} {c:<16} d={d:+d} {'OK' if u else 'X':<3} "
          f"ZPD=[{clamp(e-SIG):+.1f},{clamp(e+SIG):+.1f}] mastery={mu:.3f} θ={th:+.2f}±{se:.2f}")
print()
