from datetime import datetime, timezone
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class UserDailyModeState(Base):
    """Current, rebuildable daily learning-mode projection for one user."""

    __tablename__ = "user_daily_mode_states"
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), primary_key=True
    )
    daily_mode: Mapped[str] = mapped_column(String(16), index=True)
    duration: Mapped[str] = mapped_column(String(16))
    timezone: Mapped[str] = mapped_column(String(64))
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DailyModeEvent(Base):
    """Append-only authority for user-initiated daily-mode changes."""

    __tablename__ = "daily_mode_events"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "idempotency_key",
            name="uq_daily_mode_events_user_idempotency",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    previous_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    daily_mode: Mapped[str] = mapped_column(String(16), index=True)
    duration: Mapped[str] = mapped_column(String(16))
    timezone: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(32), index=True)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    state_version: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, index=True
    )


class UserFeedback(Base):
    """Immutable feedback fact submitted by an authenticated user."""

    __tablename__ = "user_feedback"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "idempotency_key",
            name="uq_user_feedback_user_idempotency",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    scope: Mapped[str] = mapped_column(String(24), index=True)
    feedback_type: Mapped[str] = mapped_column(String(32), index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    page_path: Mapped[str] = mapped_column(String(500), default="/")
    view: Mapped[str] = mapped_column(String(40), default="")
    section_id: Mapped[str | None] = mapped_column(
        ForeignKey("sections.id"), nullable=True, index=True
    )
    content_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_versions.id"), nullable=True, index=True
    )
    block_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    block_snapshot_hash: Mapped[str] = mapped_column(String(64), default="")
    source_mode: Mapped[str] = mapped_column(String(40))
    schema_version: Mapped[str] = mapped_column(String(40), default="feedback_v1")
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    context_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, index=True
    )


class ProductEvent(Base):
    """Append-only, allowlisted product telemetry from an authenticated web client."""

    __tablename__ = "product_events"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "event_id",
            name="uq_product_events_user_event",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    event_id: Mapped[str] = mapped_column(String(80))
    session_id: Mapped[str] = mapped_column(String(80), index=True)
    event_name: Mapped[str] = mapped_column(String(64), index=True)
    page_path: Mapped[str] = mapped_column(String(500), default="/")
    view: Mapped[str] = mapped_column(String(40), default="")
    entity_type: Mapped[str] = mapped_column(String(40), default="")
    entity_id: Mapped[str] = mapped_column(String(160), default="")
    properties_json: Mapped[str] = mapped_column(Text, default="{}")
    request_hash: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(24), default="web")
    schema_version: Mapped[str] = mapped_column(String(24), default="product_event_v1")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, index=True
    )


class UserProfile(Base):
    __tablename__ = "user_profiles"
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), primary_key=True
    )
    profession: Mapped[str] = mapped_column(String(120), default="")
    stage: Mapped[str] = mapped_column(String(40), default="")
    purpose: Mapped[str] = mapped_column(Text, default="")
    domains_json: Mapped[str] = mapped_column(Text, default="[]")
    experience: Mapped[str] = mapped_column(Text, default="")
    weekly_minutes: Mapped[int] = mapped_column(Integer, default=0)
    target_date: Mapped[str] = mapped_column(String(10), default="")
    preferences_json: Mapped[str] = mapped_column(Text, default="{}")
    version: Mapped[int] = mapped_column(Integer, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now
    )


class UserProfileRevision(Base):
    __tablename__ = "user_profile_revisions"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "version",
            name="uq_user_profile_revisions_user_version",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    snapshot_json: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(40), default="self_report")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now
    )


class UserOnboarding(Base):
    __tablename__ = "user_onboardings"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "flow_id",
            name="uq_user_onboardings_user_flow",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    flow_id: Mapped[str] = mapped_column(String(80))
    flow_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default="required", index=True)
    current_step: Mapped[str] = mapped_column(String(80), default="identity")
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now
    )


class UserIdentity(Base):
    __tablename__ = "user_identities"
    __table_args__ = (
        UniqueConstraint(
            "issuer",
            "subject",
            name="uq_user_identities_issuer_subject",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    issuer: Mapped[str] = mapped_column(String(500))
    subject: Mapped[str] = mapped_column(String(500))
    email_snapshot: Mapped[str] = mapped_column(String(320), default="")
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LocalCredential(Base):
    """Username/password credential; the legacy table name is retained."""

    __tablename__ = "local_credentials"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"),
        unique=True,
        index=True,
    )
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_token_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PrivacyConsent(Base):
    """Append-only acceptance of a specific privacy and trial notice version."""

    __tablename__ = "privacy_consents"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "notice_version",
            "trial_terms_version",
            name="uq_privacy_consents_user_versions",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    notice_version: Mapped[str] = mapped_column(String(40), index=True)
    trial_terms_version: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(24), default="accepted", index=True)
    source: Mapped[str] = mapped_column(String(40), default="in_app")
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, index=True
    )
    withdrawn_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AccountExitRequest(Base):
    """Auditable request to close an account and remove personal data."""

    __tablename__ = "account_exit_requests"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="requested", index=True)
    policy_version: Mapped[str] = mapped_column(String(40), index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, index=True
    )
    deletion_due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class OidcLoginState(Base):
    __tablename__ = "oidc_login_states"
    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(160))
    code_verifier: Mapped[str] = mapped_column(String(160))
    return_to: Mapped[str] = mapped_column(String(1000), default="/")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Shelf(Base):
    __tablename__ = "shelves"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    domain: Mapped[str] = mapped_column(String(100))
    specialty: Mapped[str] = mapped_column(String(120), default="")
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    origin: Mapped[str] = mapped_column(String(32), default="user_created", index=True)


class LearningPlan(Base):
    __tablename__ = "learning_plans"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    shelf_id: Mapped[str] = mapped_column(ForeignKey("shelves.id"), index=True)
    topic: Mapped[str] = mapped_column(String(160))
    role: Mapped[str] = mapped_column(String(80))
    experience: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text, default="")
    depth: Mapped[str] = mapped_column(String(24))
    details: Mapped[str] = mapped_column(Text, default="")
    assumptions_json: Mapped[str] = mapped_column(Text, default="[]")
    confidence: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LearningMissionVersion(Base):
    """Immutable statement of why a plan exists and what success means."""

    __tablename__ = "learning_mission_versions"
    __table_args__ = (
        UniqueConstraint(
            "plan_id",
            "version",
            name="uq_learning_mission_versions_plan_version",
        ),
        UniqueConstraint(
            "plan_id",
            "payload_hash",
            name="uq_learning_mission_versions_plan_payload",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("learning_plans.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), index=True)
    why: Mapped[str] = mapped_column(Text)
    target_capabilities_json: Mapped[str] = mapped_column(Text, default="[]")
    constraints_json: Mapped[str] = mapped_column(Text, default="{}")
    out_of_scope_json: Mapped[str] = mapped_column(Text, default="[]")
    assumptions_json: Mapped[str] = mapped_column(Text, default="[]")
    learner_context_json: Mapped[str] = mapped_column(Text, default="{}")
    inferred_fields_json: Mapped[str] = mapped_column(Text, default="[]")
    provenance_json: Mapped[str] = mapped_column(Text, default="{}")
    schema_version: Mapped[str] = mapped_column(String(40), default="mission_v1")
    payload_hash: Mapped[str] = mapped_column(String(64))
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("learning_mission_versions.id"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MissionSuccessCriterion(Base):
    __tablename__ = "mission_success_criteria"
    __table_args__ = (
        UniqueConstraint(
            "plan_id",
            "stable_key",
            name="uq_mission_success_criteria_plan_key",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("learning_plans.id"), index=True)
    stable_key: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class MissionSuccessCriterionVersion(Base):
    __tablename__ = "mission_success_criterion_versions"
    __table_args__ = (
        UniqueConstraint(
            "mission_version_id",
            "success_criterion_id",
            name="uq_mission_criterion_versions_identity",
        ),
        UniqueConstraint(
            "mission_version_id",
            "position",
            name="uq_mission_criterion_versions_position",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    mission_version_id: Mapped[str] = mapped_column(
        ForeignKey("learning_mission_versions.id"), index=True
    )
    success_criterion_id: Mapped[str] = mapped_column(
        ForeignKey("mission_success_criteria.id"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    statement: Mapped[str] = mapped_column(Text)
    acceptance_json: Mapped[str] = mapped_column(Text, default="{}")
    provenance_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Concept(Base):
    """Stable platform identity for one concept, independent of any book."""

    __tablename__ = "concepts"
    __table_args__ = (
        UniqueConstraint("namespace", "concept_key", name="uq_concepts_namespace_key"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    namespace: Mapped[str] = mapped_column(String(80), index=True)
    concept_key: Mapped[str] = mapped_column(String(200))
    canonical_name: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    origin: Mapped[str] = mapped_column(String(40), default="platform")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ConceptRevision(Base):
    """Immutable meaning of a concept at one revision."""

    __tablename__ = "concept_revisions"
    __table_args__ = (
        UniqueConstraint(
            "concept_id", "revision", name="uq_concept_revisions_concept_revision"
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    concept_id: Mapped[str] = mapped_column(ForeignKey("concepts.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(300))
    definition: Mapped[str] = mapped_column(Text)
    scope_json: Mapped[str] = mapped_column(Text, default="{}")
    boundaries_json: Mapped[str] = mapped_column(Text, default="[]")
    provenance_mode: Mapped[str] = mapped_column(String(40), default="platform")
    verification_status: Mapped[str] = mapped_column(
        String(32), default="unverified", index=True
    )
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("concept_revisions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LearningObjective(Base):
    """Immutable observable outcome used by contracts and assessments."""

    __tablename__ = "learning_objectives"
    __table_args__ = (
        UniqueConstraint(
            "namespace", "objective_key", name="uq_learning_objectives_namespace_key"
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    namespace: Mapped[str] = mapped_column(String(80), index=True)
    objective_key: Mapped[str] = mapped_column(String(200))
    statement: Mapped[str] = mapped_column(Text)
    cognitive_verb: Mapped[str] = mapped_column(String(40), default="demonstrate")
    outcome_type: Mapped[str] = mapped_column(String(40), default="knowledge")
    provenance_mode: Mapped[str] = mapped_column(String(40), default="platform")
    verification_status: Mapped[str] = mapped_column(
        String(32), default="unverified", index=True
    )
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("learning_objectives.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CurriculumSourceVersion(Base):
    """Immutable official-source metadata for one curriculum baseline input."""

    __tablename__ = "curriculum_source_versions"
    __table_args__ = (
        UniqueConstraint(
            "source_key",
            "version_label",
            name="uq_curriculum_source_key_version",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_key: Mapped[str] = mapped_column(String(160), index=True)
    source_type: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(Text)
    authority: Mapped[str] = mapped_column(String(240))
    url: Mapped[str] = mapped_column(Text)
    version_label: Mapped[str] = mapped_column(String(160))
    publication_date: Mapped[str] = mapped_column(String(32), default="")
    applicability_json: Mapped[str] = mapped_column(Text, default="{}")
    retrieval_date: Mapped[str] = mapped_column(String(32))
    content_digest: Mapped[str] = mapped_column(String(64))
    verification_status: Mapped[str] = mapped_column(
        String(32), default="candidate", index=True
    )
    provenance_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Discipline(Base):
    """Stable discipline identity shared by versioned curriculum baselines."""

    __tablename__ = "disciplines"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    code: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(240))
    jurisdiction: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProgramVersion(Base):
    """One institution- and year-specific training-program version."""

    __tablename__ = "program_versions"
    __table_args__ = (
        UniqueConstraint(
            "institution",
            "program_code",
            "version_label",
            name="uq_program_institution_code_version",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    discipline_id: Mapped[str] = mapped_column(
        ForeignKey("disciplines.id"), index=True
    )
    source_version_id: Mapped[str] = mapped_column(
        ForeignKey("curriculum_source_versions.id"), index=True
    )
    institution: Mapped[str] = mapped_column(String(240))
    program_code: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(300))
    version_label: Mapped[str] = mapped_column(String(160))
    applicability_json: Mapped[str] = mapped_column(Text, default="{}")
    review_status: Mapped[str] = mapped_column(
        String(32), default="candidate", index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CourseVersion(Base):
    """Versioned course scope; never treated as a universal course definition."""

    __tablename__ = "course_versions"
    __table_args__ = (
        UniqueConstraint(
            "program_version_id",
            "course_code",
            "version_label",
            name="uq_course_program_code_version",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    program_version_id: Mapped[str] = mapped_column(
        ForeignKey("program_versions.id"), index=True
    )
    course_code: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(300))
    version_label: Mapped[str] = mapped_column(String(160))
    course_type: Mapped[str] = mapped_column(String(80), default="")
    credits_json: Mapped[str] = mapped_column(Text, default="{}")
    assessment_json: Mapped[str] = mapped_column(Text, default="{}")
    aliases_json: Mapped[str] = mapped_column(Text, default="[]")
    review_status: Mapped[str] = mapped_column(
        String(32), default="candidate", index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Competency(Base):
    """Stable, observable capability named by a curriculum baseline."""

    __tablename__ = "competencies"
    __table_args__ = (
        UniqueConstraint(
            "namespace",
            "competency_key",
            name="uq_competency_namespace_key",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    namespace: Mapped[str] = mapped_column(String(160), index=True)
    competency_key: Mapped[str] = mapped_column(String(160))
    statement: Mapped[str] = mapped_column(Text)
    competency_type: Mapped[str] = mapped_column(String(40))
    verification_modes_json: Mapped[str] = mapped_column(Text, default="[]")
    review_status: Mapped[str] = mapped_column(
        String(32), default="candidate", index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CurriculumBaselineVersion(Base):
    """Reviewed or candidate curriculum graph used as a planning authority."""

    __tablename__ = "curriculum_baseline_versions"
    __table_args__ = (
        UniqueConstraint(
            "baseline_key",
            "version",
            name="uq_curriculum_baseline_key_version",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    baseline_key: Mapped[str] = mapped_column(String(160), index=True)
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    discipline_id: Mapped[str] = mapped_column(
        ForeignKey("disciplines.id"), index=True
    )
    program_version_id: Mapped[str] = mapped_column(
        ForeignKey("program_versions.id"), index=True
    )
    course_version_id: Mapped[str] = mapped_column(
        ForeignKey("course_versions.id"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="candidate", index=True)
    scope_json: Mapped[str] = mapped_column(Text, default="{}")
    graph_json: Mapped[str] = mapped_column(Text)
    gaps_json: Mapped[str] = mapped_column(Text, default="[]")
    source_version_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    review_json: Mapped[str] = mapped_column(Text, default="{}")
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SeriesCurriculumBaselineBinding(Base):
    """Immutable record of the published baseline selected for one Series."""

    __tablename__ = "series_curriculum_baseline_bindings"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    series_id: Mapped[str] = mapped_column(
        ForeignKey("series.id"), unique=True, index=True
    )
    baseline_version_id: Mapped[str] = mapped_column(
        ForeignKey("curriculum_baseline_versions.id"), index=True
    )
    selection_reason: Mapped[str] = mapped_column(Text)
    selection_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChapterCurriculumObjectiveBinding(Base):
    """Exact baseline-objective coverage claimed by a planned chapter."""

    __tablename__ = "chapter_curriculum_objective_bindings"
    __table_args__ = (
        UniqueConstraint(
            "chapter_id",
            "objective_key",
            name="uq_chapter_curriculum_objective",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id"), index=True)
    baseline_version_id: Mapped[str] = mapped_column(
        ForeignKey("curriculum_baseline_versions.id"), index=True
    )
    objective_key: Mapped[str] = mapped_column(String(160), index=True)
    coverage_role: Mapped[str] = mapped_column(String(32), default="teaches")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class KnowledgeGraphRelease(Base):
    """Versioned, fail-closed publication boundary for a reviewed knowledge slice."""

    __tablename__ = "knowledge_graph_releases"
    __table_args__ = (
        UniqueConstraint(
            "baseline_version_id",
            "version",
            name="uq_knowledge_graph_release_baseline_version",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    baseline_version_id: Mapped[str] = mapped_column(
        ForeignKey("curriculum_baseline_versions.id"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="candidate", index=True)
    manifest_json: Mapped[str] = mapped_column(Text)
    gaps_json: Mapped[str] = mapped_column(Text, default="[]")
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    review_json: Mapped[str] = mapped_column(Text, default="{}")
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class KnowledgeSourceVersion(Base):
    """Immutable public or licensed source snapshot for platform knowledge facts."""

    __tablename__ = "knowledge_source_versions"
    __table_args__ = (
        UniqueConstraint(
            "source_key",
            "version_label",
            name="uq_knowledge_source_key_version",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_key: Mapped[str] = mapped_column(String(160), index=True)
    source_kind: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(Text)
    authority: Mapped[str] = mapped_column(String(240))
    url: Mapped[str] = mapped_column(Text)
    version_label: Mapped[str] = mapped_column(String(200))
    retrieval_date: Mapped[str] = mapped_column(String(32))
    content_digest: Mapped[str] = mapped_column(String(64))
    rights_status: Mapped[str] = mapped_column(String(32), index=True)
    verification_status: Mapped[str] = mapped_column(
        String(32), default="candidate", index=True
    )
    provenance_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ConceptRelationVersion(Base):
    """Typed relation between exact concept revisions in one graph release."""

    __tablename__ = "concept_relation_versions"
    __table_args__ = (
        UniqueConstraint(
            "release_id",
            "from_concept_revision_id",
            "to_concept_revision_id",
            "relation_type",
            name="uq_concept_relation_release_identity",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    release_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_graph_releases.id"), index=True
    )
    from_concept_revision_id: Mapped[str] = mapped_column(
        ForeignKey("concept_revisions.id"), index=True
    )
    to_concept_revision_id: Mapped[str] = mapped_column(
        ForeignKey("concept_revisions.id"), index=True
    )
    relation_type: Mapped[str] = mapped_column(String(40), index=True)
    relation_revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default="candidate", index=True)
    provenance_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ConceptObjectiveBinding(Base):
    """Explicit curriculum outcome served by one exact concept revision."""

    __tablename__ = "concept_objective_bindings"
    __table_args__ = (
        UniqueConstraint(
            "release_id",
            "concept_revision_id",
            "learning_objective_id",
            name="uq_concept_objective_release_identity",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    release_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_graph_releases.id"), index=True
    )
    concept_revision_id: Mapped[str] = mapped_column(
        ForeignKey("concept_revisions.id"), index=True
    )
    learning_objective_id: Mapped[str] = mapped_column(
        ForeignKey("learning_objectives.id"), index=True
    )
    binding_role: Mapped[str] = mapped_column(String(32), default="teaches")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class KnowledgeClaimBinding(Base):
    """Claim-level support from a knowledge source, independent of lesson content."""

    __tablename__ = "knowledge_claim_bindings"
    __table_args__ = (
        UniqueConstraint(
            "release_id",
            "source_claim_version_id",
            "knowledge_source_version_id",
            "locator_hash",
            name="uq_knowledge_claim_binding_identity",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    release_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_graph_releases.id"), index=True
    )
    source_claim_version_id: Mapped[str] = mapped_column(
        ForeignKey("source_claim_versions.id"), index=True
    )
    knowledge_source_version_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_source_versions.id"), index=True
    )
    locator_type: Mapped[str] = mapped_column(String(32))
    locator_json: Mapped[str] = mapped_column(Text)
    locator_hash: Mapped[str] = mapped_column(String(64))
    excerpt_hash: Mapped[str] = mapped_column(String(64), default="")
    support_type: Mapped[str] = mapped_column(String(24))
    verification_status: Mapped[str] = mapped_column(String(32), index=True)
    review_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PlanCreationRequest(Base):
    __tablename__ = "plan_creation_requests"
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True, index=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), index=True)
    series_id: Mapped[str | None] = mapped_column(ForeignKey("series.id"), nullable=True)
    error_code: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MilestonePath(Base):
    __tablename__ = "milestone_paths"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    series_id: Mapped[str] = mapped_column(ForeignKey("series.id"), unique=True, index=True)
    goal_profile_version: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default="proposed", index=True)
    definition_json: Mapped[str] = mapped_column(Text)
    ruleset_version: Mapped[str] = mapped_column(String(40), default="milestone_v1")
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MilestonePathRevision(Base):
    __tablename__ = "milestone_path_revisions"
    __table_args__ = (
        UniqueConstraint(
            "path_id",
            "version",
            name="uq_milestone_path_revisions_path_version",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    path_id: Mapped[str] = mapped_column(ForeignKey("milestone_paths.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    snapshot_json: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Series(Base):
    __tablename__ = "series"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("learning_plans.id"), unique=True)
    shelf_id: Mapped[str] = mapped_column(ForeignKey("shelves.id"), index=True)
    title: Mapped[str] = mapped_column(String(240))
    rationale: Mapped[str] = mapped_column(Text)
    # Nullable only during the staged M1 -> M2 rollout. The migration backfills
    # every existing row and all new writes set it in the creation transaction.
    initial_mission_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("learning_mission_versions.id"), nullable=True, index=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class Book(Base):
    __tablename__ = "books"
    __table_args__ = (UniqueConstraint("series_id", "position"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    series_id: Mapped[str] = mapped_column(ForeignKey("series.id"), index=True)
    shelf_id: Mapped[str] = mapped_column(ForeignKey("shelves.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(240))
    topic: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text)
    estimated_minutes: Mapped[int] = mapped_column(Integer)
    outline_status: Mapped[str] = mapped_column(
        String(24), default="confirmed", index=True
    )
    outline_version: Mapped[int] = mapped_column(Integer, default=1)
    outline_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class Chapter(Base):
    __tablename__ = "chapters"
    __table_args__ = (UniqueConstraint("book_id", "position"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(240))
    objective: Mapped[str] = mapped_column(Text)
    knowledge_identity_scope_json: Mapped[str] = mapped_column(Text, default="{}")


class Section(Base):
    __tablename__ = "sections"
    __table_args__ = (UniqueConstraint("chapter_id", "position"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(240))
    question: Mapped[str] = mapped_column(Text)
    objectives_json: Mapped[str] = mapped_column(Text)


class LearningContractVersion(Base):
    """Immutable semantic contract for one section version."""

    __tablename__ = "learning_contract_versions"
    __table_args__ = (
        UniqueConstraint(
            "section_id", "version", name="uq_learning_contract_versions_section_version"
        ),
        UniqueConstraint(
            "section_id",
            "contract_hash",
            name="uq_learning_contract_versions_section_hash",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"), index=True)
    mission_version_id: Mapped[str] = mapped_column(
        ForeignKey("learning_mission_versions.id"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    section_question_snapshot: Mapped[str] = mapped_column(Text)
    target_depth: Mapped[str] = mapped_column(String(32), default="standard")
    boundaries_json: Mapped[str] = mapped_column(Text, default="[]")
    generation_context_json: Mapped[str] = mapped_column(Text, default="{}")
    provenance_mode: Mapped[str] = mapped_column(String(40), default="native_m2")
    lineage_status: Mapped[str] = mapped_column(String(32), default="verified", index=True)
    contract_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LearningContractConcept(Base):
    __tablename__ = "learning_contract_concepts"
    __table_args__ = (
        UniqueConstraint(
            "contract_version_id",
            "concept_revision_id",
            name="uq_learning_contract_concepts_identity",
        ),
        UniqueConstraint(
            "contract_version_id",
            "position",
            name="uq_learning_contract_concepts_position",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    contract_version_id: Mapped[str] = mapped_column(
        ForeignKey("learning_contract_versions.id"), index=True
    )
    concept_revision_id: Mapped[str] = mapped_column(
        ForeignKey("concept_revisions.id"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(24), default="primary")
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LearningContractObjective(Base):
    __tablename__ = "learning_contract_objectives"
    __table_args__ = (
        UniqueConstraint(
            "contract_version_id",
            "learning_objective_id",
            name="uq_learning_contract_objectives_identity",
        ),
        UniqueConstraint(
            "contract_version_id",
            "position",
            name="uq_learning_contract_objectives_position",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    contract_version_id: Mapped[str] = mapped_column(
        ForeignKey("learning_contract_versions.id"), index=True
    )
    learning_objective_id: Mapped[str] = mapped_column(
        ForeignKey("learning_objectives.id"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(24), default="primary")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LearningRun(Base):
    __tablename__ = "learning_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    series_id: Mapped[str] = mapped_column(ForeignKey("series.id"), index=True)
    initial_mission_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("learning_mission_versions.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(24), index=True, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        Index("uq_learning_runs_id_user", "id", "user_id", unique=True),
        Index(
            "uq_learning_runs_active_user_series",
            "user_id",
            "series_id",
            unique=True,
            sqlite_where=(status == "active"),
            postgresql_where=(status == "active"),
        ),
    )


class LearningRunSectionBinding(Base):
    """Frozen contract/content choice for a run after real learning activity."""

    __tablename__ = "learning_run_section_bindings"
    __table_args__ = (
        UniqueConstraint(
            "learning_run_id",
            "section_id",
            name="uq_learning_run_section_bindings_run_section",
        ),
        ForeignKeyConstraint(
            ["learning_run_id", "user_id"],
            ["learning_runs.id", "learning_runs.user_id"],
            name="fk_learning_run_section_bindings_run_user",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    learning_run_id: Mapped[str] = mapped_column(
        ForeignKey("learning_runs.id"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"), index=True)
    learning_contract_version_id: Mapped[str] = mapped_column(
        ForeignKey("learning_contract_versions.id"), index=True
    )
    content_version_id: Mapped[str] = mapped_column(
        ForeignKey("content_versions.id"), index=True
    )
    initial_quiz_set_id: Mapped[str | None] = mapped_column(
        ForeignKey("quiz_sets.id"), nullable=True
    )
    first_read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(40))
    source_fact_id: Mapped[str] = mapped_column(String(160), default="")
    lineage_audit_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MissionAdoptionEvent(Base):
    __tablename__ = "mission_adoption_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["learning_run_id", "user_id"],
            ["learning_runs.id", "learning_runs.user_id"],
            name="fk_mission_adoption_events_run_user",
        ),
        UniqueConstraint(
            "learning_run_id",
            "user_id",
            "idempotency_key",
            name="uq_mission_adoption_events_idempotency",
        ),
    )
    sequence: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    id: Mapped[str] = mapped_column(String, unique=True, index=True)
    learning_run_id: Mapped[str] = mapped_column(
        ForeignKey("learning_runs.id"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    mission_version_id: Mapped[str] = mapped_column(
        ForeignKey("learning_mission_versions.id"), index=True
    )
    previous_mission_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("learning_mission_versions.id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(Text, default="")
    idempotency_key: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class BookProgress(Base):
    __tablename__ = "book_progress"
    __table_args__ = (
        UniqueConstraint("learning_run_id", "book_id"),
        ForeignKeyConstraint(
            ["learning_run_id", "user_id"],
            ["learning_runs.id", "learning_runs.user_id"],
            name="fk_book_progress_run_user",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    learning_run_id: Mapped[str] = mapped_column(ForeignKey("learning_runs.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="locked")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChapterProgress(Base):
    __tablename__ = "chapter_progress"
    __table_args__ = (
        UniqueConstraint("learning_run_id", "chapter_id"),
        ForeignKeyConstraint(
            ["learning_run_id", "user_id"],
            ["learning_runs.id", "learning_runs.user_id"],
            name="fk_chapter_progress_run_user",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    learning_run_id: Mapped[str] = mapped_column(ForeignKey("learning_runs.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="locked")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SectionProgress(Base):
    __tablename__ = "section_progress"
    __table_args__ = (
        UniqueConstraint("learning_run_id", "section_id"),
        ForeignKeyConstraint(
            ["learning_run_id", "user_id"],
            ["learning_runs.id", "learning_runs.user_id"],
            name="fk_section_progress_run_user",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    learning_run_id: Mapped[str] = mapped_column(ForeignKey("learning_runs.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="locked")
    best_score: Mapped[int] = mapped_column(Integer, default=0)
    total_score: Mapped[int] = mapped_column(Integer, default=0)
    ask_me_unlocked: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ContentVersion(Base):
    __tablename__ = "content_versions"
    __table_args__ = (
        UniqueConstraint(
            "section_id",
            "version",
            name="uq_content_versions_section_version",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"), index=True)
    learning_contract_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("learning_contract_versions.id"), nullable=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    blocks_json: Mapped[str] = mapped_column(Text)
    sources_json: Mapped[str] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(String(16))
    publication_status: Mapped[str] = mapped_column(
        String(24), default="published", index=True
    )
    schema_version: Mapped[str] = mapped_column(String(48), default="legacy")
    prompt_version: Mapped[str] = mapped_column(String(48), default="legacy")
    generation_mode: Mapped[str] = mapped_column(
        String(32), default="model_only", index=True
    )
    rights_status: Mapped[str] = mapped_column(
        String(32), default="not_applicable", index=True
    )
    factual_status: Mapped[str] = mapped_column(
        String(32), default="unreviewed", index=True
    )
    ai_generated: Mapped[bool] = mapped_column(Boolean, default=True)
    generation_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_runs.id"), nullable=True, index=True
    )
    output_hash: Mapped[str] = mapped_column(String(64), default="")
    labeling_metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SourceVerification(Base):
    __tablename__ = "source_verifications"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    content_version_id: Mapped[str] = mapped_column(ForeignKey("content_versions.id"), unique=True)
    report_json: Mapped[str] = mapped_column(Text)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SourceVersion(Base):
    """Immutable source snapshot used by claim-level support bindings."""

    __tablename__ = "source_versions"
    __table_args__ = (
        UniqueConstraint(
            "content_version_id",
            "position",
            name="uq_source_versions_content_position",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    content_version_id: Mapped[str] = mapped_column(
        ForeignKey("content_versions.id"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(String(32))
    version_label: Mapped[str] = mapped_column(String(200), default="")
    provenance_mode: Mapped[str] = mapped_column(
        String(40), default="native_m2", index=True
    )
    reachability_status: Mapped[str] = mapped_column(
        String(32), default="unknown", index=True
    )
    verification_report_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ContentBlockVersion(Base):
    """Normalized immutable block; the M1 block id remains its stable anchor."""

    __tablename__ = "content_block_versions"
    __table_args__ = (
        UniqueConstraint(
            "content_version_id",
            "position",
            name="uq_content_block_versions_content_position",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    content_version_id: Mapped[str] = mapped_column(
        ForeignKey("content_versions.id"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    block_version: Mapped[int] = mapped_column(Integer, default=1)
    format_kind: Mapped[str] = mapped_column(String(24))
    semantic_role: Mapped[str] = mapped_column(String(32), index=True)
    heading: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[str] = mapped_column(Text)
    source_indexes_json: Mapped[str] = mapped_column(Text, default="[]")
    factuality_class: Mapped[str] = mapped_column(
        String(40), default="unspecified"
    )
    trust_state: Mapped[str] = mapped_column(
        String(32), default="model_synthesis", index=True
    )
    generation_method: Mapped[str] = mapped_column(
        String(40), default="ai_generated"
    )
    assessment_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_block_versions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ContentBlockAssessmentTarget(Base):
    """Explicit v2 teaching binding; never inferred from block prose."""

    __tablename__ = "content_block_assessment_targets"
    __table_args__ = (
        UniqueConstraint(
            "content_block_version_id",
            "assessment_target_id",
            name="uq_content_block_assessment_target_identity",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    content_block_version_id: Mapped[str] = mapped_column(
        ForeignKey("content_block_versions.id"), index=True
    )
    assessment_target_id: Mapped[str] = mapped_column(
        ForeignKey("assessment_targets.id"), index=True
    )
    binding_role: Mapped[str] = mapped_column(String(32), default="teaches")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SourceClaim(Base):
    """Stable identity for a versioned atomic source claim."""

    __tablename__ = "source_claims"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    stable_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SourceClaimVersion(Base):
    __tablename__ = "source_claim_versions"
    __table_args__ = (
        UniqueConstraint(
            "source_claim_id",
            "version",
            name="uq_source_claim_versions_claim_version",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_claim_id: Mapped[str] = mapped_column(
        ForeignKey("source_claims.id"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    statement: Mapped[str] = mapped_column(Text)
    claim_kind: Mapped[str] = mapped_column(String(40), index=True)
    scope_json: Mapped[str] = mapped_column(Text, default="{}")
    strict: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    trust_state: Mapped[str] = mapped_column(
        String(32), default="unverified", index=True
    )
    generation_method: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(24), default="candidate", index=True)
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_claim_versions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ContentBlockClaimAnchor(Base):
    __tablename__ = "content_block_claim_anchors"
    __table_args__ = (
        UniqueConstraint(
            "content_block_version_id",
            "source_claim_version_id",
            name="uq_content_block_claim_anchors_identity",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    content_block_version_id: Mapped[str] = mapped_column(
        ForeignKey("content_block_versions.id"), index=True
    )
    source_claim_version_id: Mapped[str] = mapped_column(
        ForeignKey("source_claim_versions.id"), index=True
    )
    anchor_role: Mapped[str] = mapped_column(String(32), default="states")
    locator_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SourceClaimBinding(Base):
    __tablename__ = "source_claim_bindings"
    __table_args__ = (
        UniqueConstraint(
            "source_claim_version_id",
            "source_version_id",
            "locator_hash",
            name="uq_source_claim_bindings_claim_source_locator",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_claim_version_id: Mapped[str] = mapped_column(
        ForeignKey("source_claim_versions.id"), index=True
    )
    source_version_id: Mapped[str] = mapped_column(
        ForeignKey("source_versions.id"), index=True
    )
    locator_type: Mapped[str] = mapped_column(String(32))
    locator_json: Mapped[str] = mapped_column(Text)
    locator_hash: Mapped[str] = mapped_column(String(64))
    excerpt_text: Mapped[str] = mapped_column(Text, default="")
    excerpt_hash: Mapped[str] = mapped_column(String(64), default="")
    support_type: Mapped[str] = mapped_column(String(24))
    verification_mode: Mapped[str] = mapped_column(String(32))
    verification_status: Mapped[str] = mapped_column(String(32), index=True)
    verification_rule_version: Mapped[str] = mapped_column(String(40))
    report_json: Mapped[str] = mapped_column(Text, default="{}")
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class KnowledgeGap(Base):
    """Immutable detected gap; lifecycle changes live in append-only events."""

    __tablename__ = "knowledge_gaps"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    gap_type: Mapped[str] = mapped_column(String(40), index=True)
    severity: Mapped[str] = mapped_column(String(24), index=True)
    subject_kind: Mapped[str] = mapped_column(String(40))
    source_claim_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_claim_versions.id"), nullable=True, index=True
    )
    content_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_versions.id"), nullable=True, index=True
    )
    content_block_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_block_versions.id"), nullable=True, index=True
    )
    detector_kind: Mapped[str] = mapped_column(String(32))
    detector_rule_version: Mapped[str] = mapped_column(String(40))
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class KnowledgeGapEvent(Base):
    __tablename__ = "knowledge_gap_events"
    __table_args__ = (
        UniqueConstraint(
            "knowledge_gap_id",
            "idempotency_key",
            name="uq_knowledge_gap_events_idempotency",
        ),
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String, unique=True, index=True)
    knowledge_gap_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_gaps.id"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    actor_kind: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[str] = mapped_column(String(160), default="")
    rationale: Mapped[str] = mapped_column(Text, default="")
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    rule_version: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class GovernanceDecisionSnapshot(Base):
    """Append-only explanation of a content or quiz publication decision."""

    __tablename__ = "governance_decision_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "decision_scope",
            "idempotency_key",
            name="uq_governance_decision_scope_idempotency",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    decision_scope: Mapped[str] = mapped_column(String(32), index=True)
    content_version_id: Mapped[str] = mapped_column(
        ForeignKey("content_versions.id"), index=True
    )
    quiz_set_id: Mapped[str | None] = mapped_column(
        ForeignKey("quiz_sets.id"), nullable=True, index=True
    )
    learning_contract_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("learning_contract_versions.id"), nullable=True, index=True
    )
    requested_mode: Mapped[str] = mapped_column(String(24))
    mode: Mapped[str] = mapped_column(String(24), index=True)
    allowed: Mapped[bool] = mapped_column(Boolean)
    assessment_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    rule_version: Mapped[str] = mapped_column(String(40))
    input_hash: Mapped[str] = mapped_column(String(64))
    actor_kind: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[str] = mapped_column(String(160), default="")
    idempotency_key: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class GenerationRun(Base):
    """GenerationAttempt audit authority; table name is retained for migration compatibility."""

    __tablename__ = "generation_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"), index=True)
    operation: Mapped[str] = mapped_column(String(32))
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), index=True)
    model: Mapped[str] = mapped_column(String(160), default="")
    pipeline_version: Mapped[str] = mapped_column(String(48), default="legacy")
    prompt_version: Mapped[str] = mapped_column(String(48), default="legacy")
    schema_version: Mapped[str] = mapped_column(String(48), default="legacy")
    generation_mode: Mapped[str] = mapped_column(String(32), default="model_only")
    context_hash: Mapped[str] = mapped_column(String(64), default="")
    trace_json: Mapped[str] = mapped_column(Text, default="{}")
    error_code: Mapped[str] = mapped_column(String(80), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AiInvocation(Base):
    """One physical request sent to an AI provider."""

    __tablename__ = "ai_invocations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    api_mode: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(160), index=True)
    operation: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    usage_status: Mapped[str] = mapped_column(String(24), index=True)
    attribution_status: Mapped[str] = mapped_column(String(32), index=True)
    actor_kind: Mapped[str] = mapped_column(String(32), default="")
    actor_id: Mapped[str] = mapped_column(String(160), default="")
    subject_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"),
        index=True,
        nullable=True,
    )
    provider_response_id: Mapped[str] = mapped_column(String(200), default="")
    error_code: Mapped[str] = mapped_column(String(80), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metering_schema_version: Mapped[str] = mapped_column(String(24), default="v1")


class AiUsageMeasurement(Base):
    """Append-only observation of usage for an invocation."""

    __tablename__ = "ai_usage_measurements"
    __table_args__ = (
        UniqueConstraint(
            "invocation_id",
            "source",
            "measurement_version",
            name="uq_ai_usage_measurement_source_version",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    invocation_id: Mapped[str] = mapped_column(
        ForeignKey("ai_invocations.id"),
        index=True,
    )
    source: Mapped[str] = mapped_column(String(32), index=True)
    quality: Mapped[str] = mapped_column(String(24), index=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_5m_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_1h_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_usage_json: Mapped[str] = mapped_column(Text, default="{}")
    measurement_version: Mapped[str] = mapped_column(String(24), default="v1")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class GenerationLease(Base):
    """Database-backed cross-request mutex with crash recovery."""

    __tablename__ = "generation_leases"
    resource_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(80), unique=True)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class QuizSet(Base):
    __tablename__ = "quiz_sets"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"), index=True)
    content_version_id: Mapped[str] = mapped_column(ForeignKey("content_versions.id"), index=True)
    learning_contract_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("learning_contract_versions.id"), nullable=True, index=True
    )
    generation: Mapped[int] = mapped_column(Integer)
    questions_json: Mapped[str] = mapped_column(Text)
    publication_status: Mapped[str] = mapped_column(
        String(24), default="published", index=True
    )
    schema_version: Mapped[str] = mapped_column(String(48), default="legacy")


class AssessmentItemVersion(Base):
    """Normalized immutable quiz item generated with a lesson candidate."""

    __tablename__ = "assessment_item_versions"
    __table_args__ = (
        UniqueConstraint(
            "quiz_set_id",
            "position",
            name="uq_assessment_item_versions_quiz_position",
        ),
        UniqueConstraint(
            "quiz_set_id",
            "item_key",
            name="uq_assessment_item_versions_quiz_key",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    quiz_set_id: Mapped[str] = mapped_column(ForeignKey("quiz_sets.id"), index=True)
    assessment_target_id: Mapped[str] = mapped_column(
        ForeignKey("assessment_targets.id"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    item_key: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AssessmentItemEvidenceBlock(Base):
    """Question-to-taught-block evidence binding validated before publish."""

    __tablename__ = "assessment_item_evidence_blocks"
    __table_args__ = (
        UniqueConstraint(
            "assessment_item_version_id",
            "content_block_version_id",
            name="uq_assessment_item_evidence_block_identity",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    assessment_item_version_id: Mapped[str] = mapped_column(
        ForeignKey("assessment_item_versions.id"), index=True
    )
    content_block_version_id: Mapped[str] = mapped_column(
        ForeignKey("content_block_versions.id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class QuizAttempt(Base):
    __tablename__ = "quiz_attempts"
    __table_args__ = (
        UniqueConstraint(
            "learning_run_id",
            "user_id",
            "idempotency_key",
            name="uq_quiz_attempts_run_user_idempotency",
        ),
        ForeignKeyConstraint(
            ["learning_run_id", "user_id"],
            ["learning_runs.id", "learning_runs.user_id"],
            name="fk_quiz_attempts_run_user",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    quiz_set_id: Mapped[str] = mapped_column(ForeignKey("quiz_sets.id"))
    learning_contract_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("learning_contract_versions.id"), nullable=True, index=True
    )
    content_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_versions.id"), nullable=True, index=True
    )
    learning_run_id: Mapped[str] = mapped_column(ForeignKey("learning_runs.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_hash: Mapped[str] = mapped_column(String(64), default="")
    answers_json: Mapped[str] = mapped_column(Text)
    results_json: Mapped[str] = mapped_column(Text)
    passed: Mapped[bool] = mapped_column(Boolean)
    workflow_status: Mapped[str] = mapped_column(String(24), default="processing")
    response_json: Mapped[str] = mapped_column(Text, default="")
    workflow_error_code: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AssessmentTarget(Base):
    """Server-owned measurement identity; AI-generated questions only reference it."""

    __tablename__ = "assessment_targets"
    __table_args__ = (
        UniqueConstraint(
            "objective_key",
            "dimension",
            "target_depth",
            name="uq_assessment_targets_semantics",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    # Nullable during the staged M1 -> M2 rollout. Migration 0030 backfills every
    # existing target with explicit provisional identities without changing its id.
    concept_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("concept_revisions.id"), nullable=True, index=True
    )
    learning_objective_id: Mapped[str | None] = mapped_column(
        ForeignKey("learning_objectives.id"), nullable=True, index=True
    )
    objective_key: Mapped[str] = mapped_column(String(300))
    objective_statement: Mapped[str] = mapped_column(Text)
    dimension: Mapped[str] = mapped_column(String(32), default="recognition")
    target_depth: Mapped[str] = mapped_column(String(32), default="standard")
    identity_status: Mapped[str] = mapped_column(
        String(32), default="legacy_provisional", index=True
    )
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SectionAssessmentTarget(Base):
    """Contract-local gate attributes for a reusable measurement identity."""

    __tablename__ = "section_assessment_targets"
    __table_args__ = (
        UniqueConstraint(
            "section_id",
            "position",
            name="uq_section_assessment_targets_position",
        ),
        UniqueConstraint(
            "section_id",
            "assessment_target_id",
            name="uq_section_assessment_targets_target",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"), index=True)
    assessment_target_id: Mapped[str] = mapped_column(
        ForeignKey("assessment_targets.id"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    verification_policy: Mapped[str] = mapped_column(
        String(40), default="choice_quiz_v1"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LearningContractAssessmentTarget(Base):
    """Contract-local gate policy for a stable assessment identity."""

    __tablename__ = "learning_contract_assessment_targets"
    __table_args__ = (
        UniqueConstraint(
            "contract_version_id",
            "position",
            name="uq_learning_contract_assessment_targets_position",
        ),
        UniqueConstraint(
            "contract_version_id",
            "assessment_target_id",
            name="uq_learning_contract_assessment_targets_target",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    contract_version_id: Mapped[str] = mapped_column(
        ForeignKey("learning_contract_versions.id"), index=True
    )
    assessment_target_id: Mapped[str] = mapped_column(
        ForeignKey("assessment_targets.id"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    verification_policy: Mapped[str] = mapped_column(
        String(40), default="choice_quiz_v1"
    )
    evidence_policy: Mapped[str] = mapped_column(
        String(40), default="assessment_evidence_v1"
    )
    diagnostic_only: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ScoringResult(Base):
    """Immutable deterministic scoring fact for one submitted attempt."""

    __tablename__ = "scoring_results"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("quiz_attempts.id"), unique=True, index=True
    )
    scoring_rule_version: Mapped[str] = mapped_column(
        String(40), default="choice_exact_v2"
    )
    score: Mapped[int] = mapped_column(Integer)
    total: Mapped[int] = mapped_column(Integer)
    passed: Mapped[bool] = mapped_column(Boolean)
    results_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AssessmentObservation(Base):
    """Append-only observation derived from a scoring fact."""

    __tablename__ = "assessment_observations"
    __table_args__ = (
        UniqueConstraint(
            "attempt_id",
            "question_index",
            name="uq_assessment_observations_attempt_question",
        ),
        ForeignKeyConstraint(
            ["learning_run_id", "user_id"],
            ["learning_runs.id", "learning_runs.user_id"],
            name="fk_assessment_observations_run_user",
        ),
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String, unique=True, index=True)
    learning_run_id: Mapped[str] = mapped_column(
        ForeignKey("learning_runs.id"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"), index=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("quiz_attempts.id"), index=True)
    quiz_set_id: Mapped[str | None] = mapped_column(
        ForeignKey("quiz_sets.id"), nullable=True, index=True
    )
    learning_contract_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("learning_contract_versions.id"), nullable=True, index=True
    )
    content_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_versions.id"), nullable=True, index=True
    )
    scoring_result_id: Mapped[str] = mapped_column(
        ForeignKey("scoring_results.id"), index=True
    )
    assessment_target_id: Mapped[str] = mapped_column(
        ForeignKey("assessment_targets.id"), index=True
    )
    question_index: Mapped[int] = mapped_column(Integer)
    correct: Mapped[bool] = mapped_column(Boolean)
    assistance_mode: Mapped[str] = mapped_column(
        String(32), default="unassisted_initial"
    )
    learning_episode_id: Mapped[str] = mapped_column(String(120), index=True)
    equivalence_group_id: Mapped[str] = mapped_column(String(120), default="", index=True)
    qualification_at_creation: Mapped[str] = mapped_column(
        String(24), default="eligible"
    )
    qualification_rule_version: Mapped[str] = mapped_column(
        String(40), default="evidence_v1"
    )
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class EvidenceQualificationEvent(Base):
    __tablename__ = "evidence_qualification_events"
    __table_args__ = (
        UniqueConstraint(
            "observation_id",
            "projection_family",
            "rule_version",
            name="uq_evidence_qualification_observation_family_rule",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    observation_id: Mapped[str] = mapped_column(
        ForeignKey("assessment_observations.id"), index=True
    )
    projection_family: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(Text, default="")
    rule_version: Mapped[str] = mapped_column(String(40), default="evidence_v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AssessmentGateState(Base):
    """Rebuildable per-run target resolution used by deterministic progression."""

    __tablename__ = "assessment_gate_states"
    __table_args__ = (
        UniqueConstraint(
            "learning_run_id",
            "section_id",
            "assessment_target_id",
            name="uq_assessment_gate_states_run_section_target",
        ),
        ForeignKeyConstraint(
            ["learning_run_id", "user_id"],
            ["learning_runs.id", "learning_runs.user_id"],
            name="fk_assessment_gate_states_run_user",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    learning_run_id: Mapped[str] = mapped_column(
        ForeignKey("learning_runs.id"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"), index=True)
    assessment_target_id: Mapped[str] = mapped_column(
        ForeignKey("assessment_targets.id"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="unresolved", index=True)
    resolved_by_observation_id: Mapped[str | None] = mapped_column(
        ForeignKey("assessment_observations.id"), nullable=True
    )
    projection_rule_version: Mapped[str] = mapped_column(
        String(40), default="gate_v1"
    )
    projection_version: Mapped[int] = mapped_column(Integer, default=1)
    source_observation_watermark: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LearningDecisionSnapshot(Base):
    """Append-only audit fact for a server-owned learning decision.

    Projection rows may be rebuilt or replaced.  This row instead freezes the
    exact rule input and output that justified a gate or progression mutation.
    A rule upgrade is a new snapshot, never an in-place rewrite.
    """

    __tablename__ = "learning_decision_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "decision_kind",
            "idempotency_key",
            name="uq_learning_decision_kind_idempotency",
        ),
        ForeignKeyConstraint(
            ["learning_run_id", "user_id"],
            ["learning_runs.id", "learning_runs.user_id"],
            name="fk_learning_decision_snapshots_run_user",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    learning_run_id: Mapped[str] = mapped_column(
        ForeignKey("learning_runs.id"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"), index=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("quiz_attempts.id"), index=True)
    learning_contract_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("learning_contract_versions.id"), nullable=True, index=True
    )
    content_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_versions.id"), nullable=True, index=True
    )
    decision_kind: Mapped[str] = mapped_column(String(32), index=True)
    trigger_kind: Mapped[str] = mapped_column(String(32), index=True)
    rule_version: Mapped[str] = mapped_column(String(40))
    input_snapshot_json: Mapped[str] = mapped_column(Text)
    output_decision_json: Mapped[str] = mapped_column(Text)
    source_observation_watermark: Mapped[int] = mapped_column(Integer, default=0)
    input_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class KnowledgeStateProjection(Base):
    __tablename__ = "knowledge_state_projections"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "assessment_target_id",
            name="uq_knowledge_state_user_target",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    assessment_target_id: Mapped[str] = mapped_column(
        ForeignKey("assessment_targets.id"), index=True
    )
    p_known_ppm: Mapped[int] = mapped_column(Integer, default=200000)
    uncertainty_ppm: Mapped[int] = mapped_column(Integer, default=1000000)
    claim_status: Mapped[str] = mapped_column(String(32), default="unobserved", index=True)
    retention_rounds: Mapped[int] = mapped_column(Integer, default=0)
    parameter_set_version: Mapped[str] = mapped_column(String(40), default="bkt_v1")
    projection_rule_version: Mapped[str] = mapped_column(String(40), default="mastery_v1")
    projection_version: Mapped[int] = mapped_column(Integer, default=1)
    source_observation_watermark: Mapped[int] = mapped_column(Integer, default=0)
    rebuilt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ReviewState(Base):
    __tablename__ = "review_states"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "assessment_target_id",
            name="uq_review_states_user_target",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    assessment_target_id: Mapped[str] = mapped_column(
        ForeignKey("assessment_targets.id"), index=True
    )
    status: Mapped[str] = mapped_column(String(24), default="scheduled", index=True)
    next_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    priority: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(80), default="initial_learning")
    spacing_stage: Mapped[int] = mapped_column(Integer, default=0)
    projection_rule_version: Mapped[str] = mapped_column(String(40), default="review_v1")
    projection_version: Mapped[int] = mapped_column(Integer, default=1)
    source_observation_watermark: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ReviewSelectionRun(Base):
    """Immutable daily selection input/output boundary for review assignments."""

    __tablename__ = "review_selection_runs"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "selection_date",
            "rule_version",
            name="uq_review_selection_runs_user_day_rule",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    selection_date: Mapped[str] = mapped_column(String(10), index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    daily_budget: Mapped[int] = mapped_column(Integer)
    due_count: Mapped[int] = mapped_column(Integer)
    rule_version: Mapped[str] = mapped_column(String(40), index=True)
    input_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ReviewAssignment(Base):
    """Server-owned delayed-review authorization and lifecycle state."""

    __tablename__ = "review_assignments"
    __table_args__ = (
        UniqueConstraint(
            "selection_run_id",
            "assessment_target_id",
            name="uq_review_assignments_selection_target",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    selection_run_id: Mapped[str] = mapped_column(
        ForeignKey("review_selection_runs.id"), index=True
    )
    review_state_id: Mapped[str] = mapped_column(
        ForeignKey("review_states.id"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    assessment_target_id: Mapped[str] = mapped_column(
        ForeignKey("assessment_targets.id"), index=True
    )
    source_learning_run_id: Mapped[str] = mapped_column(
        ForeignKey("learning_runs.id"), index=True
    )
    source_section_id: Mapped[str] = mapped_column(
        ForeignKey("sections.id"), index=True
    )
    learning_contract_version_id: Mapped[str] = mapped_column(
        ForeignKey("learning_contract_versions.id"), index=True
    )
    content_version_id: Mapped[str] = mapped_column(
        ForeignKey("content_versions.id"), index=True
    )
    prior_quiz_set_id: Mapped[str] = mapped_column(
        ForeignKey("quiz_sets.id"), index=True
    )
    review_quiz_set_id: Mapped[str | None] = mapped_column(
        ForeignKey("quiz_sets.id"), nullable=True, unique=True
    )
    submitted_attempt_id: Mapped[str | None] = mapped_column(
        ForeignKey("quiz_attempts.id"), nullable=True, unique=True
    )
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    base_priority: Mapped[int] = mapped_column(Integer)
    effective_priority: Mapped[int] = mapped_column(Integer)
    selection_rule_version: Mapped[str] = mapped_column(String(40))
    qualification_rule_version: Mapped[str] = mapped_column(String(40))
    prior_item_signatures_json: Mapped[str] = mapped_column(Text, default="[]")
    item_signatures_json: Mapped[str] = mapped_column(Text, default="[]")
    response_json: Mapped[str] = mapped_column(Text, default="")
    last_event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ReviewAssignmentEventRecord(Base):
    """Append-only audit event for a review assignment transition."""

    __tablename__ = "review_assignment_events"
    __table_args__ = (
        UniqueConstraint(
            "assignment_id",
            "event_type",
            name="uq_review_assignment_events_type",
        ),
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String, unique=True, index=True)
    assignment_id: Mapped[str] = mapped_column(
        ForeignKey("review_assignments.id"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(24), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rule_version: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str] = mapped_column(String(160), default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Remediation(Base):
    __tablename__ = "remediations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"), index=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("quiz_attempts.id"), index=True)
    replacement_quiz_id: Mapped[str] = mapped_column(ForeignKey("quiz_sets.id"), unique=True)
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("remediations.id"), nullable=True, unique=True
    )
    blocks_json: Mapped[str] = mapped_column(Text)
    objectives_json: Mapped[str] = mapped_column(Text)
    strategy: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class QaSession(Base):
    __tablename__ = "qa_sessions"
    __table_args__ = (
        UniqueConstraint(
            "learning_run_id",
            "section_id",
            "user_id",
            name="uq_qa_sessions_run_section_user",
        ),
        ForeignKeyConstraint(
            ["learning_run_id", "user_id"],
            ["learning_runs.id", "learning_runs.user_id"],
            name="fk_qa_sessions_run_user",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    learning_run_id: Mapped[str] = mapped_column(ForeignKey("learning_runs.id"), index=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    learning_contract_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("learning_contract_versions.id"), nullable=True, index=True
    )
    content_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_versions.id"), nullable=True, index=True
    )
    daily_mode: Mapped[str] = mapped_column(String(16), default="slow")
    memory_json: Mapped[str] = mapped_column(Text, default="{}")


class QaMessage(Base):
    __tablename__ = "qa_messages"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("qa_sessions.id"), index=True)
    thread_id: Mapped[str] = mapped_column(String, index=True)
    block_id: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class QaThread(Base):
    __tablename__ = "qa_threads"
    __table_args__ = (UniqueConstraint("session_id", "thread_id"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("qa_sessions.id"), index=True)
    thread_id: Mapped[str] = mapped_column(String, index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    classification: Mapped[str] = mapped_column(String(24), default="new_question")
    corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LearningNote(Base):
    __tablename__ = "learning_notes"
    __table_args__ = (
        UniqueConstraint(
            "learning_run_id",
            "section_id",
            "user_id",
            name="uq_learning_notes_run_section_user",
        ),
        ForeignKeyConstraint(
            ["learning_run_id", "user_id"],
            ["learning_runs.id", "learning_runs.user_id"],
            name="fk_learning_notes_run_user",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    learning_run_id: Mapped[str] = mapped_column(ForeignKey("learning_runs.id"), index=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    learning_contract_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("learning_contract_versions.id"), nullable=True, index=True
    )
    content_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_versions.id"), nullable=True, index=True
    )
    ai_content_json: Mapped[str] = mapped_column(Text)
    user_content_json: Mapped[str] = mapped_column(Text, default="{}")
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LearningNoteSummary(Base):
    """Immutable post-learning summary bound to the facts used to create it."""

    __tablename__ = "learning_note_summaries"
    __table_args__ = (
        UniqueConstraint(
            "note_id",
            "version",
            name="uq_learning_note_summaries_note_version",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    note_id: Mapped[str] = mapped_column(ForeignKey("learning_notes.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content_json: Mapped[str] = mapped_column(Text)
    source_content_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_versions.id"), nullable=True
    )
    learning_contract_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("learning_contract_versions.id"), nullable=True, index=True
    )
    source_contract_version: Mapped[str] = mapped_column(
        String(40), default="generated_note_v1"
    )
    source_observation_watermark: Mapped[int] = mapped_column(Integer, default=0)
    generation_rule_version: Mapped[str] = mapped_column(
        String(40), default="note_summary_v1"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LearningNoteReviewSupplement(Base):
    """Append-only knowledge added by a completed review episode."""

    __tablename__ = "learning_note_review_supplements"
    __table_args__ = (
        UniqueConstraint(
            "note_id",
            "review_episode_id",
            name="uq_learning_note_review_supplements_episode",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    note_id: Mapped[str] = mapped_column(ForeignKey("learning_notes.id"), index=True)
    review_episode_id: Mapped[str] = mapped_column(String(120), index=True)
    content_json: Mapped[str] = mapped_column(Text)
    author_kind: Mapped[str] = mapped_column(String(24), default="user")
    source_observation_watermark: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LearningNoteUserRevision(Base):
    """User-owned presentation/content layer; only user actions create versions."""

    __tablename__ = "learning_note_user_revisions"
    __table_args__ = (
        UniqueConstraint(
            "note_id",
            "version",
            name="uq_learning_note_user_revisions_note_version",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    note_id: Mapped[str] = mapped_column(ForeignKey("learning_notes.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content_json: Mapped[str] = mapped_column(Text)
    based_on_summary_version: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(String(24), default="user_edit")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LearningTask(Base):
    """Durable AI work created by a committed learning fact.

    The task row is orchestration state, never the authority for quiz results,
    progression, generated content, or learning evidence.
    """

    __tablename__ = "learning_tasks"
    __table_args__ = (
        UniqueConstraint(
            "learning_run_id",
            "task_type",
            "idempotency_key",
            name="uq_learning_tasks_run_type_idempotency",
        ),
        ForeignKeyConstraint(
            ["learning_run_id", "user_id"],
            ["learning_runs.id", "learning_runs.user_id"],
            name="fk_learning_tasks_run_user",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    learning_run_id: Mapped[str] = mapped_column(
        ForeignKey("learning_runs.id"),
        index=True,
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    section_id: Mapped[str | None] = mapped_column(
        ForeignKey("sections.id"),
        index=True,
        nullable=True,
    )
    task_type: Mapped[str] = mapped_column(String(40), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    trigger_id: Mapped[str] = mapped_column(String(160), index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(24), index=True, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    error_code: Mapped[str] = mapped_column(String(80), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    lease_owner: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    lease_token: Mapped[str | None] = mapped_column(String(160), nullable=True, unique=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LearningEvidence(Base):
    __tablename__ = "learning_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["learning_run_id", "user_id"],
            ["learning_runs.id", "learning_runs.user_id"],
            name="fk_learning_evidence_run_user",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    learning_run_id: Mapped[str] = mapped_column(ForeignKey("learning_runs.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    shelf_id: Mapped[str] = mapped_column(ForeignKey("shelves.id"), index=True)
    series_id: Mapped[str] = mapped_column(ForeignKey("series.id"), index=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id"), index=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id"), index=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"), index=True)
    concept: Mapped[str] = mapped_column(String(300), index=True)
    evidence_type: Mapped[str] = mapped_column(String(32))
    result_json: Mapped[str] = mapped_column(Text)
    mastery_delta: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LearningMemory(Base):
    __tablename__ = "learning_memory"
    __table_args__ = (UniqueConstraint("user_id", "shelf_id", "concept"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    shelf_id: Mapped[str] = mapped_column(ForeignKey("shelves.id"), index=True)
    concept: Mapped[str] = mapped_column(String(300), index=True)
    mastery_score: Mapped[int] = mapped_column(Integer, default=0)
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AskMeSession(Base):
    __tablename__ = "ask_me_sessions"
    __table_args__ = (
        UniqueConstraint(
            "learning_run_id",
            "section_id",
            "user_id",
            name="uq_ask_me_sessions_run_section_user",
        ),
        ForeignKeyConstraint(
            ["learning_run_id", "user_id"],
            ["learning_runs.id", "learning_runs.user_id"],
            name="fk_ask_me_sessions_run_user",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    learning_run_id: Mapped[str] = mapped_column(ForeignKey("learning_runs.id"), index=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    learning_contract_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("learning_contract_versions.id"), nullable=True, index=True
    )
    content_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_versions.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(24), default="active")
    round_index: Mapped[int] = mapped_column(Integer, default=0)
    entries_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AskMeDiscussionSession(Base):
    __tablename__ = "ask_me_discussion_sessions"
    __table_args__ = (
        UniqueConstraint(
            "learning_run_id",
            "section_id",
            "user_id",
            name="uq_ask_me_discussion_sessions_run_section_user",
        ),
        ForeignKeyConstraint(
            ["learning_run_id", "user_id"],
            ["learning_runs.id", "learning_runs.user_id"],
            name="fk_ask_me_discussion_sessions_run_user",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    learning_run_id: Mapped[str] = mapped_column(
        ForeignKey("learning_runs.id"), index=True
    )
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    learning_contract_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("learning_contract_versions.id"), nullable=True, index=True
    )
    content_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_versions.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    active_topic_id: Mapped[str] = mapped_column(String, default="")
    pending_turn_id: Mapped[str] = mapped_column(String, default="")
    schema_version: Mapped[str] = mapped_column(String(40), default="ask_me_v2")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AskMeDiscussionTopic(Base):
    __tablename__ = "ask_me_discussion_topics"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "position",
            name="uq_ask_me_discussion_topics_position",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("ask_me_discussion_sessions.id"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    purpose: Mapped[str] = mapped_column(Text)
    dimension: Mapped[str] = mapped_column(String(32), index=True)
    assessment_target_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    current_prompt: Mapped[str] = mapped_column(Text)
    turn_count: Mapped[int] = mapped_column(Integer, default=0)
    evidence_recorded: Mapped[bool] = mapped_column(Boolean, default=False)
    final_assessment_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AskMeDiscussionTurnRecord(Base):
    __tablename__ = "ask_me_discussion_turns"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "idempotency_key",
            name="uq_ask_me_discussion_turns_user_idempotency",
        ),
        UniqueConstraint(
            "topic_id",
            "turn_index",
            name="uq_ask_me_discussion_turns_topic_index",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("ask_me_discussion_sessions.id"), index=True
    )
    topic_id: Mapped[str] = mapped_column(
        ForeignKey("ask_me_discussion_topics.id"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    turn_index: Mapped[int] = mapped_column(Integer)
    prompt: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    evaluation: Mapped[str] = mapped_column(String(24), default="")
    feedback_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(24), default="processing", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_json: Mapped[str] = mapped_column(Text, default="")
    error_code: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AskMeDiscussionCommand(Base):
    __tablename__ = "ask_me_discussion_commands"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "idempotency_key",
            name="uq_ask_me_discussion_commands_user_idempotency",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("ask_me_discussion_sessions.id"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    command_type: Mapped[str] = mapped_column(String(32), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChapterPractice(Base):
    __tablename__ = "chapter_practices"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id"), unique=True)
    title: Mapped[str] = mapped_column(String(240))
    instructions_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class BookCapstone(Base):
    __tablename__ = "book_capstones"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id"), unique=True)
    title: Mapped[str] = mapped_column(String(240))
    brief_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ArtifactProgress(Base):
    __tablename__ = "artifact_progress"
    __table_args__ = (
        UniqueConstraint("learning_run_id", "target_type", "target_id"),
        ForeignKeyConstraint(
            ["learning_run_id", "user_id"],
            ["learning_runs.id", "learning_runs.user_id"],
            name="fk_artifact_progress_run_user",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    learning_run_id: Mapped[str] = mapped_column(ForeignKey("learning_runs.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    target_type: Mapped[str] = mapped_column(String(32), index=True)
    target_id: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String(24), default="locked")
    submission_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ArtifactAttachment(Base):
    __tablename__ = "artifact_attachments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["learning_run_id", "user_id"],
            ["learning_runs.id", "learning_runs.user_id"],
            name="fk_artifact_attachments_run_user",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    learning_run_id: Mapped[str] = mapped_column(ForeignKey("learning_runs.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    target_type: Mapped[str] = mapped_column(String(32), index=True)
    target_id: Mapped[str] = mapped_column(String, index=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(160), default="application/octet-stream")
    byte_size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    object_key: Mapped[str] = mapped_column(String(600), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ArtifactSubmission(Base):
    """Immutable user submission fact; ArtifactProgress is its projection."""

    __tablename__ = "artifact_submissions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["learning_run_id", "user_id"],
            ["learning_runs.id", "learning_runs.user_id"],
            name="fk_artifact_submissions_run_user",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    learning_run_id: Mapped[str] = mapped_column(ForeignKey("learning_runs.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    target_type: Mapped[str] = mapped_column(String(32), index=True)
    target_id: Mapped[str] = mapped_column(String, index=True)
    content_json: Mapped[str] = mapped_column(Text)
    attachment_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class LearningResumePosition(Base):
    __tablename__ = "learning_resume_positions"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "learning_run_id",
            name="uq_learning_resume_user_run",
        ),
        ForeignKeyConstraint(
            ["learning_run_id", "user_id"],
            ["learning_runs.id", "learning_runs.user_id"],
            name="fk_learning_resume_run_user",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    learning_run_id: Mapped[str] = mapped_column(ForeignKey("learning_runs.id"), index=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"), index=True)
    learning_contract_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("learning_contract_versions.id"), nullable=True, index=True
    )
    content_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_versions.id"), nullable=True, index=True
    )
    block_id: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class ChapterRevision(Base):
    __tablename__ = "chapter_revisions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id"), index=True)
    action: Mapped[str] = mapped_column(String(24))
    before_json: Mapped[str] = mapped_column(Text, default="{}")
    after_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    mode: Mapped[str] = mapped_column(String(24))
    model: Mapped[str] = mapped_column(String(160), default="")
    status: Mapped[str] = mapped_column(String(24), index=True)
    code_version: Mapped[str] = mapped_column(String(80), default="")
    prompt_version: Mapped[str] = mapped_column(String(80), default="v1")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
