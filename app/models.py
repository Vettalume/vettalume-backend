"""The locked Vettalume data model (Phase 0).

Enum-like fields are stored as plain strings for portability; the Python enums below are the
source of truth and are validated at the API boundary (schemas.py). They can be hardened into
native Postgres enums later without changing application code.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .types import JSONType


# ---- controlled vocabularies (validated in schemas.py) ----
class Context(str, enum.Enum):
    diagnostic = "diagnostic"
    practice = "practice"
    sectional_mock = "sectional_mock"
    full_mock = "full_mock"


# "Cold" = exam conditions. ONLY these responses are admissible for IRT calibration.
# Practice is taught-first/untimed, so it never calibrates item difficulty (it drives the MAB).
COLD_CONTEXTS = {Context.diagnostic.value, Context.sectional_mock.value, Context.full_mock.value}
MOCK_CONTEXTS = {Context.sectional_mock.value, Context.full_mock.value}


class ItemFormat(str, enum.Enum):
    mcq = "mcq"
    tita = "tita"  # type-in-the-answer (no guessing floor, no negative marking)


class UsageScope(str, enum.Enum):
    both = "both"
    mock_only = "mock_only"        # reserved holdout: never served in practice
    practice_only = "practice_only"


class NodeKind(str, enum.Enum):
    topic = "topic"
    concept = "concept"  # subtopic / leaf concept


# ---- account & entitlements ----
class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Entitlement(Base):
    __tablename__ = "entitlements"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    exam_code: Mapped[str] = mapped_column(ForeignKey("exams.code"))
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("account_id", "exam_code", name="uq_entitlement"),)


# ---- exam catalog ----
class Exam(Base):
    __tablename__ = "exams"
    code: Mapped[str] = mapped_column(String(16), primary_key=True)  # CAT / GMAT / GRE
    name: Mapped[str] = mapped_column(String(120))


class Section(Base):
    __tablename__ = "sections"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    exam_code: Mapped[str] = mapped_column(ForeignKey("exams.code"))
    key: Mapped[str] = mapped_column(String(32))  # QA / VARC / DILR ...
    name: Mapped[str] = mapped_column(String(120))
    __table_args__ = (UniqueConstraint("exam_code", "key", name="uq_section"),)


# ---- knowledge graph: tree (parent_id) + DAG (PrereqEdge) ----
class KnowledgeNode(Base):
    __tablename__ = "knowledge_nodes"
    # FK columns aren't auto-indexed in Postgres; the syllabus tree is walked by exam and by
    # parent_id (children lookup) on every /learn/overview and /admin/syllabus build.
    __table_args__ = (
        Index("ix_knodes_exam", "exam_code"),
        Index("ix_knodes_parent", "parent_id"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # authored, e.g. 'avg-simple'
    exam_code: Mapped[str] = mapped_column(ForeignKey("exams.code"))
    section_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sections.id"))
    kind: Mapped[str] = mapped_column(String(16))  # NodeKind
    name: Mapped[str] = mapped_column(String(160))
    parent_id: Mapped[Optional[str]] = mapped_column(ForeignKey("knowledge_nodes.id"), nullable=True)
    theory: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sort_order: Mapped[int] = mapped_column(Integer, default=0)   # admin display order (lower = earlier)


class PrereqEdge(Base):
    __tablename__ = "prereq_edges"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    node_id: Mapped[str] = mapped_column(ForeignKey("knowledge_nodes.id"))          # this node ...
    prereq_node_id: Mapped[str] = mapped_column(ForeignKey("knowledge_nodes.id"))   # ... needs this
    __table_args__ = (UniqueConstraint("node_id", "prereq_node_id", name="uq_prereq"),)


# ---- the shared item bank ----
class Item(Base):
    """Phase-0 simplification: item_id is the PK (one row per item) and `version` bumps on a
    content change (which also nulls the calibration fields, since changed content must be
    re-calibrated). The production-correct design is IMMUTABLE (item_id, version) rows that
    responses bind to; that machinery is added in Phase 2, when calibration makes version
    binding load-bearing. The response already snapshots item_version, so the seam exists."""
    __tablename__ = "items"
    # The single hottest filter in the app: `WHERE concept_node_id = ? [AND status='approved']`
    # runs on practice / quiz / mastery / item-count paths. Without these, Postgres seq-scans the
    # whole items table each time (compounded across the N+1 loops). exam_code serves the overview scan.
    __table_args__ = (
        Index("ix_items_concept_status", "concept_node_id", "status"),
        Index("ix_items_exam", "exam_code"),
    )
    item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str] = mapped_column(String(64))

    # routing tags (controlled vocabulary -> FKs)
    exam_code: Mapped[str] = mapped_column(ForeignKey("exams.code"))
    section_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sections.id"))
    concept_node_id: Mapped[str] = mapped_column(ForeignKey("knowledge_nodes.id"))
    archetype_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # TODO Phase-1: FK
    grid_cell: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)     # e.g. 'CALC-L2'

    # authored psychometrics & format
    difficulty_d: Mapped[int] = mapped_column(Integer)  # 1..5, expert-set, never shown to learner
    format: Mapped[str] = mapped_column(String(8))      # ItemFormat
    num_options: Mapped[int] = mapped_column(Integer, default=4)
    negative_marking: Mapped[bool] = mapped_column(Boolean, default=False)

    # payload
    stem: Mapped[str] = mapped_column(Text)
    options: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    correct_answer: Mapped[str] = mapped_column(String(255))
    distractor_map: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    solution: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    time_benchmark_s: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # provenance & scope
    provenance: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    usage_scope: Mapped[str] = mapped_column(String(16), default=UsageScope.both.value)
    passage_set_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="approved", index=True)

    # DERIVED — written by calibration only, NEVER authored via the Excel
    irt_a: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    irt_b: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    irt_c: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    empirical: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    calibration_run_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    calibrated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---- the event spine (append-only) ----
class Response(Base):
    __tablename__ = "responses"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    learner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("items.item_id"), index=True)
    item_version: Mapped[int] = mapped_column(Integer)
    context: Mapped[str] = mapped_column(String(16), index=True)  # THE discriminator (Context)
    correct: Mapped[bool] = mapped_column(Boolean)
    answer_given: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    response_time_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    hints_used: Mapped[int] = mapped_column(Integer, default=0)
    difficulty_d: Mapped[int] = mapped_column(Integer)  # snapshot
    exam_code: Mapped[str] = mapped_column(String(16), index=True)   # denormalized for calib scans
    section_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    @property
    def admissible_for_calibration(self) -> bool:
        return self.context in COLD_CONTEXTS


# ---- exposure ledger (drives the shared-bank eligibility rule) ----
class Exposure(Base):
    __tablename__ = "exposure"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    learner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("items.item_id"), index=True)
    last_seen_context: Mapped[str] = mapped_column(String(16))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    times_seen: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (UniqueConstraint("learner_id", "item_id", name="uq_exposure"),)


# ---- per-learner per-node state (mastery store) ----
class LearnerNodeState(Base):
    __tablename__ = "learner_node_state"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    learner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("knowledge_nodes.id"), index=True)
    learned: Mapped[bool] = mapped_column(Boolean, default=False)
    performance_p: Mapped[float] = mapped_column(Float, default=0.0)
    difficulty_score: Mapped[float] = mapped_column(Float, default=0.0)
    memory_strength: Mapped[float] = mapped_column(Float, default=0.0)
    mastery: Mapped[float] = mapped_column(Float, default=0.0)
    # Phase 1 stores bandit weights / reward history / MCM traces here as JSON.
    bandit_state: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    # learning-page engagement signals for the subtopic progress %: {"read": bool, "watched": bool}
    engagement: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (UniqueConstraint("learner_id", "node_id", name="uq_learner_node"),)


# ============================================================================
# Phase 2 — psychometric IRT: versioned parameter store + ability estimates
# ============================================================================
class CalibrationRun(Base):
    """One execution of the calibration worker. Every IrtParameter row points back to the run that
    produced it, so the parameter store is fully versioned and auditable."""
    __tablename__ = "calibration_runs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    exam_code: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(16), default="complete")  # complete | failed | gated
    n_items: Mapped[int] = mapped_column(Integer, default=0)
    n_responses: Mapped[int] = mapped_column(Integer, default=0)
    n_learners: Mapped[int] = mapped_column(Integer, default=0)
    iterations: Mapped[int] = mapped_column(Integer, default=0)
    converged: Mapped[bool] = mapped_column(Boolean, default=False)
    activated: Mapped[bool] = mapped_column(Boolean, default=False)  # did it become the live params?
    summary: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)  # phase counts, gate, notes
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True)


class IrtParameter(Base):
    """A calibrated (a, b, c) for one item from one run. The row with active=True is the live
    parameter set the mock scorer/selector uses; older rows are retained for rollback and drift."""
    __tablename__ = "irt_parameters"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calibration_runs.id"), index=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("items.item_id"), index=True)
    a: Mapped[float] = mapped_column(Float)
    b: Mapped[float] = mapped_column(Float)
    c: Mapped[float] = mapped_column(Float)
    phase: Mapped[str] = mapped_column(String(8))          # "b" | "2pl" | "3pl"
    n_responses: Mapped[int] = mapped_column(Integer, default=0)
    se_b: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("run_id", "item_id", name="uq_run_item"),)


class AbilityEstimate(Base):
    """A scored ability (theta) on the -3..+3 scale for a learner over a cold/mock scope, with its SE.
    Append-only: a learner accrues estimates over diagnostics and mocks."""
    __tablename__ = "ability_estimates"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    learner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    exam_code: Mapped[str] = mapped_column(String(16), index=True)
    scope: Mapped[str] = mapped_column(String(64))         # "diagnostic" | session_id | "full_mock" ...
    theta: Mapped[float] = mapped_column(Float)
    se: Mapped[float] = mapped_column(Float)
    n_items: Mapped[int] = mapped_column(Integer, default=0)
    method: Mapped[str] = mapped_column(String(8), default="eap")   # "eap" | "elo"
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True)


# ============================================================================
# Phase 3 — mocks: per-response checkpointed session (delivery + scoring state)
# ============================================================================
class MockSession(Base):
    """A mock attempt. This row IS the reliability checkpoint: it is upserted after every single
    response, so a dropped connection loses nothing — resume reads the latest state. It holds the
    live ability (theta/se), the delivery plan (fixed form sequence / MST panels), and the cursor."""
    __tablename__ = "mock_sessions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    learner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    exam_code: Mapped[str] = mapped_column(String(16), index=True)
    section_key: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    mode: Mapped[str] = mapped_column(String(16))         # item_adaptive | mst | fixed_form
    status: Mapped[str] = mapped_column(String(16), default="in_progress")  # in_progress|completed|abandoned
    stage: Mapped[str] = mapped_column(String(16), default="main")          # routing|panel|main (MST)
    panel_taken: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # easy|medium|hard
    cursor: Mapped[int] = mapped_column(Integer, default=0)                 # fixed/MST position
    theta: Mapped[float] = mapped_column(Float, default=0.0)
    se: Mapped[float] = mapped_column(Float, default=99.0)
    reliability: Mapped[float] = mapped_column(Float, default=0.0)
    n_answered: Mapped[int] = mapped_column(Integer, default=0)
    max_items: Mapped[int] = mapped_column(Integer, default=25)
    se_target: Mapped[float] = mapped_column(Float, default=0.30)
    plan: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)   # form/panels + served list
    seed: Mapped[int] = mapped_column(Integer, default=0)                   # exposure RNG seed
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


# ============================================================================
# Phase 4 — plan engine: versioned study plans (each re-plan is a new version, diffed vs the prior)
# ============================================================================
class StudyPlan(Base):
    """A study plan generated from a diagnosis. Versions accumulate per (learner, exam) so the plan
    engine can diff a re-plan against the prior version and explain the change in plain language."""
    __tablename__ = "study_plans"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    learner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    exam_code: Mapped[str] = mapped_column(String(16), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | superseded
    items: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)      # ordered plan items
    diagnosis: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)  # snapshot it was built from
    rationale: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)  # diff + plain-language change
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


# ============================================================================
# Phase 5 — billing/entitlements + Honest-Perimeter accuracy record (new tables only;
# the existing Entitlement table is reused, with status encoding the tier: free|active|expired)
# ============================================================================
class PricePlan(Base):
    """SKU catalog. A plan is either a per-course tier (free|paid) or a multi-exam bundle.
    Multi-currency (USD for GMAT/GRE, INR for CAT) lives here without forking the platform (BL-01)."""
    __tablename__ = "price_plans"
    code: Mapped[str] = mapped_column(String(48), primary_key=True)   # e.g. 'gmat_summit'
    kind: Mapped[str] = mapped_column(String(16))                     # free | paid | bundle
    exam_code: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # None for bundles
    name: Mapped[str] = mapped_column(String(120))
    currency: Mapped[str] = mapped_column(String(8))                  # USD | INR
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)     # minor units (cents/paise)
    period: Mapped[str] = mapped_column(String(16), default="one_time")
    bundle_exams: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)  # for bundles
    limits: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)        # free-tier caps
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Order(Base):
    """A recorded purchase. This is the billing-records layer (eligibility + claim state, BL-05);
    it does NOT move money — a real PSP (Stripe/Razorpay) integrates at billing.purchase()."""
    __tablename__ = "orders"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    plan_code: Mapped[str] = mapped_column(String(48))
    currency: Mapped[str] = mapped_column(String(8))
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="paid")   # paid | refunded
    claim_state: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)  # guarantee/refund
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


class PaymentOrder(Base):
    """Tracks one Razorpay order through its lifecycle. The id IS the Razorpay order id, so the
    webhook (which carries that id) can look the row up directly. Separate from Order so the
    additive-only schema rule is respected (no new columns on existing tables)."""
    __tablename__ = "payment_orders"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)        # razorpay order_id (order_...)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    plan_code: Mapped[str] = mapped_column(String(48))
    amount_paise: Mapped[int] = mapped_column(Integer)                   # minor units charged
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    status: Mapped[str] = mapped_column(String(16), default="created")  # created | paid | failed
    rp_payment_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    granted: Mapped[bool] = mapped_column(Boolean, default=False)        # access granted? (idempotency guard)
    coupon_code: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)  # applied coupon (usage counted on grant)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Subscription(Base):
    """Time-bound access to one exam, created when a payment is verified. Expiry lives here (the
    Entitlement table can't gain columns), and the access guard treats a paid entitlement as expired
    once its subscription lapses. Renewals extend expires_at."""
    __tablename__ = "subscriptions"
    # Access guards look up "this account's subscription for this exam" on paid endpoints.
    __table_args__ = (Index("ix_subs_acct_exam", "account_id", "exam_code"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    exam_code: Mapped[str] = mapped_column(ForeignKey("exams.code"))
    plan_code: Mapped[str] = mapped_column(String(48))
    order_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # the PaymentOrder/razorpay id
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)               # naive UTC; compared with utcnow()
    status: Mapped[str] = mapped_column(String(16), default="active")    # active | expired | cancelled
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EmailOtp(Base):
    """One pending email-verification code per account. Codes are stored HASHED (never plaintext).
    `last_sent_at` powers the 30s resend cooldown; `attempts` burns the code after too many wrong tries."""
    __tablename__ = "email_otps"
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime)              # naive UTC
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class LoginThrottle(Base):
    """Per-account failed-password counter for brute-force defence on /auth/login. After
    login_max_attempts wrong tries the account is locked until locked_until; a correct login clears it."""
    __tablename__ = "login_throttle"
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # naive UTC
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuthSession(Base):
    """A server-side login session. The bearer token IS the id (an opaque random string). Two
    expiries: a sliding idle window (`now - last_seen_at <= session_inactivity_days`, bumped on every
    request) and an absolute cap (`now - created_at <= session_max_days`). Active use stays signed in
    until the absolute cap; ~a day idle (sleep / away) auto-logs-out sooner."""
    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)        # opaque session token
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    # Device binding: sha256 of the User-Agent that first used this session. A token replayed from a
    # different browser (e.g. copied out of devtools) won't match and is rejected. Nullable so sessions
    # created before this feature bind on their next request.
    ua_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class AccountAuth(Base):
    """Auth metadata that can't go on the existing accounts table (additive-only): which provider the
    student signed up with, their Google subject id (for linking Google sign-ins), and when they
    accepted the terms."""
    __tablename__ = "account_auth"
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    provider: Mapped[str] = mapped_column(String(16), default="password")  # password | google
    google_sub: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    terms_accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PredictionRecord(Base):
    """One emitted prediction and (once known) its verified outcome. The aggregate over these rows
    IS the Honest Perimeter's published accuracy record: coverage of the bands and mean error."""
    __tablename__ = "prediction_records"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("accounts.id"), nullable=True, index=True)
    exam_code: Mapped[str] = mapped_column(String(16), index=True)
    kind: Mapped[str] = mapped_column(String(24))                     # score | percentile | ability
    point: Mapped[float] = mapped_column(Float)
    band_low: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    band_high: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    se: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    basis: Mapped[str] = mapped_column(String(16), default="provisional")  # calibrated | provisional
    outcome: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    within_band: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


# ============================================================================
# Phase 6 — real auth: password credentials, one-to-one with Account (new table; the accounts
# table is unchanged so existing databases need no migration)
# ============================================================================
class Credential(Base):
    """A learner's password credential (PBKDF2-SHA256 hash). Separate from Account so the existing
    accounts table is untouched and dev-login / passwordless accounts remain valid."""
    __tablename__ = "credentials"
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AdminUser(Base):
    """Content-admin role grant (additive table — no column added to `accounts`, so it works on
    existing DBs via create_all). An account is an admin iff it has a row here OR its email is in
    settings.admin_emails. Admin accounts gate ALL content-authoring endpoints; "no outsiders"."""

    __tablename__ = "admin_users"

    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), default="admin")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Coupon(Base):
    """Discount code (additive table). Field names mirror the admin console exactly so the
    frontend maps 1:1. Money fields are integers in the smallest currency unit (paise).
    `value` is a percentage when type='percentage', else a paise amount. 0 on a limit means
    'unlimited'. valid_from/valid_until are stored as the frontend's datetime-local strings."""

    __tablename__ = "coupons"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    type: Mapped[str] = mapped_column(String(16), default="percentage")  # percentage | fixed
    value: Mapped[int] = mapped_column(Integer, default=0)
    max_total: Mapped[int] = mapped_column(Integer, default=0)
    max_per_user: Mapped[int] = mapped_column(Integer, default=0)
    min_purchase: Mapped[int] = mapped_column(Integer, default=0)
    max_discount: Mapped[int] = mapped_column(Integer, default=0)
    valid_from: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    valid_until: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attempt: Mapped[str] = mapped_column(String(16), default="all")  # all | first | second | third
    courses: Mapped[list] = mapped_column(JSONType, default=list)     # exam codes; [] = all
    used: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | inactive
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StudentProfile(Base):
    """Extended student fields the admin console shows (additive table — no columns added to
    `accounts`, so it works on existing DBs via create_all). Joined to Account for email/name;
    enrollments live in Entitlement. `payment` is {status, amount, method, date}. `deleted` is a
    soft-delete flag so the admin 'remove' hides a student without wiping their learning history."""

    __tablename__ = "student_profiles"

    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    phone: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    city: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)       # self-service profile
    about: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)     # self-service profile
    target_exam: Mapped[Optional[str]] = mapped_column(String(40), nullable=True) # self-service profile
    status: Mapped[str] = mapped_column(String(16), default="active")        # active | inactive
    reg_type: Mapped[str] = mapped_column(String(16), default="registered")  # registered|trial|paid
    purchased_course: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    last_login: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    payment: Mapped[dict] = mapped_column(JSONType, default=dict)
    role: Mapped[str] = mapped_column(String(16), default="student")         # student | manager
    joined: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Material(Base):
    """A downloadable study material (PDF / slides / notes) attached to a concept node (additive
    table). File bytes are stored in the DB for a fully self-contained, portable feature — fine for
    modestly-sized study PDFs. At scale the bytes move to S3 object storage with signed URLs; only
    the storage backend changes, the table and endpoints stay. Delivery is entitlement-gated."""

    __tablename__ = "materials"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("knowledge_nodes.id"), index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    filename: Mapped[str] = mapped_column(String(255), default="")
    content_type: Mapped[str] = mapped_column(String(128), default="application/pdf")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MediaAsset(Base):
    """A question image stored by a stable key = the question/item id (taken from the uploaded
    filename, e.g. `q123.png` -> key `q123`). Served publicly at /media/{key} so it loads in an
    <img>. Bytes live in the DB — fine for modestly-sized figures; swap to object storage at scale."""

    __tablename__ = "media_assets"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    content_type: Mapped[str] = mapped_column(String(128), default="image/png")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ContactMessage(Base):
    """A "Contact us" form submission from the public site. Read + triaged by admins."""

    __tablename__ = "contact_messages"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    first_name: Mapped[str] = mapped_column(String(120), default="")
    last_name: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    handled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Mock(Base):
    """An admin-authored fixed-form mock test (additive table). `type` is 'sectional' or 'full'.
    The whole section/question structure is stored as JSON in `sections` (each section carries its
    embedded questions) so a mock is a self-contained paper — every student who takes it sees the
    same questions, which is what the admin console builds. Type-specific scalars live in columns."""

    __tablename__ = "mocks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    exam_code: Mapped[str] = mapped_column(ForeignKey("exams.code"), index=True)
    type: Mapped[str] = mapped_column(String(16))  # sectional | full
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | published
    negative: Mapped[int] = mapped_column(Integer, default=0)          # sectional negative marking
    duration: Mapped[int] = mapped_column(Integer, default=0)          # full-mock total minutes
    scoring_marks: Mapped[int] = mapped_column(Integer, default=1)     # full-mock marks per correct
    scoring_neg: Mapped[int] = mapped_column(Integer, default=0)       # full-mock negative per wrong
    instructions: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sections: Mapped[list] = mapped_column(JSONType, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DiagnosticAttempt(Base):
    """One diagnostic attempt per learner per exam — the UNIQUE constraint IS the once-only gate.
    Stores the submitted answers and the per-section ability the diagnostic produced, so the result
    is reproducible and the lock survives restarts. The diagnostic paper itself is a Mock of
    type='diagnostic'; this row records that a given learner has taken it."""
    __tablename__ = "diagnostic_attempts"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    learner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    exam_code: Mapped[str] = mapped_column(String(16), index=True)
    mock_id: Mapped[str] = mapped_column(String(40))                          # the diagnostic Mock taken
    status: Mapped[str] = mapped_column(String(16), default="in_progress")    # in_progress | completed
    answers: Mapped[dict] = mapped_column(JSONType, default=dict)             # {question_id: selected_index}
    section_ability: Mapped[dict] = mapped_column(JSONType, default=dict)     # {section: {theta, se, band, raw, total}}
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    __table_args__ = (UniqueConstraint("learner_id", "exam_code", name="uq_diagnostic_once"),)


class MockAttempt(Base):
    """A completed admin-authored mock attempt (sectional or full). Persists the submitted answers,
    per-question time, and the graded result, so the student can review one attempt and see
    aggregates across every attempt in a section. Repeatable (no unique gate) — history is kept.
    Distinct from MockSession (adaptive delivery) and DiagnosticAttempt (once-only diagnostic)."""
    __tablename__ = "mock_attempts"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    learner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    mock_id: Mapped[str] = mapped_column(String(40), index=True)   # the Mock taken (no FK: mocks may be deleted)
    exam_code: Mapped[str] = mapped_column(String(16), index=True)
    mock_type: Mapped[str] = mapped_column(String(16))             # sectional | full
    section_key: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)  # QA/VARC/DILR (sectional)
    mock_name: Mapped[str] = mapped_column(String(200), default="")
    answers: Mapped[dict] = mapped_column(JSONType, default=dict)      # {question_id: selected}
    durations: Mapped[dict] = mapped_column(JSONType, default=dict)    # {question_id: ms spent}
    section_scores: Mapped[list] = mapped_column(JSONType, default=list)  # [{name, raw, wrong, ...}]
    overall: Mapped[dict] = mapped_column(JSONType, default=dict)      # {raw, wrong, unattempted, total, ...}
    time_ms: Mapped[int] = mapped_column(Integer, default=0)           # total time taken
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True)
