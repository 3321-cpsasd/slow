import hashlib
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    AssessmentTarget,
    Book,
    Chapter,
    Concept,
    ConceptRevision,
    ContentVersion,
    LearningContractAssessmentTarget,
    LearningContractConcept,
    LearningContractObjective,
    LearningContractVersion,
    LearningMissionVersion,
    LearningObjective,
    LearningRun,
    LearningRunSectionBinding,
    QuizSet,
    Section,
    SectionAssessmentTarget,
    Series,
    now,
)


M1_NAMESPACE = "m1_provisional"
M1_CONTRACT_SCHEMA_VERSION = "learning_contract_v1"


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load(value: str, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _stable_id(prefix: str, *parts) -> str:
    material = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(material.encode()).hexdigest()[:32]}"


def _normalized(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _objective_key(statement: str) -> str:
    return hashlib.sha256(_normalized(statement).encode()).hexdigest()


def _section_objectives(section: Section) -> list[tuple[str, bool]]:
    parsed: list[tuple[str, bool | None]] = []
    for item in _load(section.objectives_json, []):
        if isinstance(item, dict):
            statement = str(item.get("statement") or item.get("objective") or "").strip()
            required = item.get("required", item.get("core"))
        else:
            statement = str(item).strip()
            required = None
        if statement:
            parsed.append((statement, bool(required) if required is not None else None))
    if not parsed:
        parsed = [(section.question.strip(), True)]

    result: list[tuple[str, bool]] = []
    positions: dict[str, int] = {}
    for position, (statement, explicit_required) in enumerate(parsed):
        key = _objective_key(statement)
        required = explicit_required if explicit_required is not None else position == 0
        if key in positions:
            old_statement, old_required = result[positions[key]]
            result[positions[key]] = (old_statement, old_required or required)
        else:
            positions[key] = len(result)
            result.append((statement, required))
    return result


def materialize_provisional_target(
    db: Session, target: AssessmentTarget
) -> AssessmentTarget:
    """Attach transparent M1 identities without changing the target's stable id."""

    if target.concept_revision_id and target.learning_objective_id:
        return target

    concept_id = _stable_id("concept_m1", target.id)
    revision_id = _stable_id("concept_revision_m1", target.id, 1)
    objective_id = _stable_id("learning_objective_m1", target.id)

    concept = db.get(Concept, concept_id)
    if not concept:
        concept = Concept(
            id=concept_id,
            namespace=M1_NAMESPACE,
            concept_key=target.id,
            canonical_name=target.objective_statement,
            status="active",
            origin="m1_provisional",
        )
        db.add(concept)
    revision = db.get(ConceptRevision, revision_id)
    if not revision:
        revision = ConceptRevision(
            id=revision_id,
            concept_id=concept_id,
            revision=1,
            label=target.objective_statement,
            definition=target.objective_statement,
            scope_json=_dump({"legacyAssessmentTargetId": target.id}),
            boundaries_json="[]",
            provenance_mode="m1_provisional",
            verification_status="provisional",
        )
        db.add(revision)
    objective = db.get(LearningObjective, objective_id)
    if not objective:
        objective = LearningObjective(
            id=objective_id,
            namespace=M1_NAMESPACE,
            objective_key=target.id,
            statement=target.objective_statement,
            cognitive_verb="demonstrate",
            outcome_type="knowledge",
            provenance_mode="m1_provisional",
            verification_status="provisional",
            status="active",
        )
        db.add(objective)
    db.flush()
    target.concept_revision_id = revision_id
    target.learning_objective_id = objective_id
    target.identity_status = "legacy_provisional"
    db.flush()
    return target


def _ensure_section_targets(
    db: Session, section: Section
) -> list[tuple[SectionAssessmentTarget, AssessmentTarget]]:
    rows = db.execute(
        select(SectionAssessmentTarget, AssessmentTarget)
        .join(
            AssessmentTarget,
            AssessmentTarget.id == SectionAssessmentTarget.assessment_target_id,
        )
        .where(SectionAssessmentTarget.section_id == section.id)
        .order_by(SectionAssessmentTarget.position)
    ).all()
    by_key = {target.objective_key: (binding, target) for binding, target in rows}

    # Existing bindings are historical server-owned semantics. Never rewrite or
    # expand a partially materialized M1 set during migration.
    objectives = [] if rows else _section_objectives(section)
    for position, (statement, required) in enumerate(objectives, 1):
        key = _objective_key(statement)
        pair = by_key.get(key)
        if pair is None:
            target = db.scalar(
                select(AssessmentTarget).where(
                    AssessmentTarget.objective_key == key,
                    AssessmentTarget.dimension == "recognition",
                    AssessmentTarget.target_depth == "standard",
                )
            )
            if target is None:
                target = AssessmentTarget(
                    id=_stable_id("target_m1", key, "recognition", "standard"),
                    objective_key=key,
                    objective_statement=statement,
                    dimension="recognition",
                    target_depth="standard",
                    identity_status="legacy_provisional",
                    status="active",
                )
                db.add(target)
                db.flush()
            binding = SectionAssessmentTarget(
                id=_stable_id("section_target_m1", section.id, target.id),
                section_id=section.id,
                assessment_target_id=target.id,
                position=position,
                required=required,
                verification_policy="choice_quiz_v1",
            )
            db.add(binding)
            db.flush()
            by_key[key] = (binding, target)

    result = sorted(by_key.values(), key=lambda pair: pair[0].position)
    for _binding, target in result:
        materialize_provisional_target(db, target)
    return result


def mission_version_for_section(db: Session, section_id: str) -> str:
    mission_version_id = db.scalar(
        select(Series.initial_mission_version_id)
        .join(Book, Book.series_id == Series.id)
        .join(Chapter, Chapter.book_id == Book.id)
        .join(Section, Section.chapter_id == Chapter.id)
        .where(Section.id == section_id)
    )
    if not mission_version_id:
        raise AppError(
            "小节缺少学习使命版本，不能建立学习契约",
            code="MISSION_VERSION_MISSING",
            status=500,
        )
    return mission_version_id


def ensure_learning_contract(
    db: Session,
    section: Section,
    *,
    mission_version_id: str | None = None,
    provenance_mode: str = "native_m2",
) -> LearningContractVersion:
    """Create an immutable contract without changing an existing section instance."""

    mission_id = mission_version_id or mission_version_for_section(db, section.id)
    mission = db.get(LearningMissionVersion, mission_id)
    mission_constraints = _load(mission.constraints_json, {}) if mission else {}
    delivery_depth = str(mission_constraints.get("depth") or "deep")
    if delivery_depth not in {"overview", "deep", "mastery"}:
        delivery_depth = "deep"
    target_rows = _ensure_section_targets(db, section)
    payload = {
        "schemaVersion": M1_CONTRACT_SCHEMA_VERSION,
        "sectionId": section.id,
        "missionVersionId": mission_id,
        "question": section.question,
        "targetDepth": delivery_depth,
        "targets": [
            {
                "assessmentTargetId": target.id,
                "conceptRevisionId": target.concept_revision_id,
                "learningObjectiveId": target.learning_objective_id,
                "position": binding.position,
                "required": binding.required,
                "verificationPolicy": binding.verification_policy,
            }
            for binding, target in target_rows
        ],
        "provenanceMode": provenance_mode,
    }
    contract_hash = hashlib.sha256(_dump(payload).encode()).hexdigest()
    existing = db.scalar(
        select(LearningContractVersion).where(
            LearningContractVersion.section_id == section.id,
            LearningContractVersion.contract_hash == contract_hash,
        )
    )
    if existing:
        return existing

    next_version = (
        db.scalar(
            select(func.max(LearningContractVersion.version)).where(
                LearningContractVersion.section_id == section.id
            )
        )
        or 0
    ) + 1
    contract = LearningContractVersion(
        id=_stable_id("learning_contract_m1", section.id, contract_hash),
        section_id=section.id,
        mission_version_id=mission_id,
        version=next_version,
        section_question_snapshot=section.question,
        target_depth=delivery_depth,
        boundaries_json="[]",
        generation_context_json=_dump(
            {
                "mode": provenance_mode,
                "sourceObjectives": _load(section.objectives_json, []),
                "targetDepth": delivery_depth,
                "contextPolicyVersion": "lesson_content_context_v1",
            }
        ),
        provenance_mode=provenance_mode,
        lineage_status="provisional",
        contract_hash=contract_hash,
    )
    db.add(contract)
    db.flush()

    seen_concepts: set[str] = set()
    seen_objectives: set[str] = set()
    for binding, target in target_rows:
        if target.concept_revision_id not in seen_concepts:
            seen_concepts.add(target.concept_revision_id)
            db.add(
                LearningContractConcept(
                    id=_stable_id(
                        "contract_concept_m1", contract.id, target.concept_revision_id
                    ),
                    contract_version_id=contract.id,
                    concept_revision_id=target.concept_revision_id,
                    position=len(seen_concepts),
                    role="primary",
                    required=binding.required,
                )
            )
        if target.learning_objective_id not in seen_objectives:
            seen_objectives.add(target.learning_objective_id)
            db.add(
                LearningContractObjective(
                    id=_stable_id(
                        "contract_objective_m1",
                        contract.id,
                        target.learning_objective_id,
                    ),
                    contract_version_id=contract.id,
                    learning_objective_id=target.learning_objective_id,
                    position=len(seen_objectives),
                    role="primary",
                )
            )
        db.add(
            LearningContractAssessmentTarget(
                id=_stable_id("contract_target_m1", contract.id, target.id),
                contract_version_id=contract.id,
                assessment_target_id=target.id,
                position=binding.position,
                required=binding.required,
                verification_policy=binding.verification_policy,
                evidence_policy="assessment_evidence_v1",
                diagnostic_only=False,
            )
        )
    db.flush()
    return contract


def ensure_m1_learning_contract(
    db: Session,
    section: Section,
    *,
    mission_version_id: str | None = None,
) -> LearningContractVersion:
    return ensure_learning_contract(
        db,
        section,
        mission_version_id=mission_version_id,
        provenance_mode="derived_from_m1",
    )


def require_run_section_binding(
    db: Session,
    *,
    learning_run_id: str,
    user_id: str,
    section_id: str,
) -> LearningRunSectionBinding:
    binding = db.scalar(
        select(LearningRunSectionBinding).where(
            LearningRunSectionBinding.learning_run_id == learning_run_id,
            LearningRunSectionBinding.user_id == user_id,
            LearningRunSectionBinding.section_id == section_id,
        )
    )
    if not binding:
        raise AppError(
            "请先打开本节以冻结正文和题目版本",
            code="SECTION_NOT_OPENED",
            status=409,
        )
    return binding


def open_run_section(
    db: Session,
    *,
    run: LearningRun,
    section: Section,
    mission_version_id: str,
    source: str,
    uid,
    preferred_quiz_id: str | None = None,
    preferred_block_id: str | None = None,
) -> LearningRunSectionBinding:
    """Freeze one exact contract/content/initial-quiz triple for a real user action."""

    existing = db.scalar(
        select(LearningRunSectionBinding).where(
            LearningRunSectionBinding.learning_run_id == run.id,
            LearningRunSectionBinding.user_id == run.user_id,
            LearningRunSectionBinding.section_id == section.id,
        )
    )
    if existing:
        return existing

    quiz = db.get(QuizSet, preferred_quiz_id) if preferred_quiz_id else None
    content = db.get(ContentVersion, quiz.content_version_id) if quiz else None
    if preferred_block_id and not content:
        candidates = db.scalars(
            select(ContentVersion)
            .join(
                LearningContractVersion,
                LearningContractVersion.id
                == ContentVersion.learning_contract_version_id,
            )
            .where(
                ContentVersion.section_id == section.id,
                ContentVersion.publication_status == "published",
                LearningContractVersion.mission_version_id == mission_version_id,
            )
            .order_by(ContentVersion.version)
        ).all()
        content = next(
            (
                item
                for item in candidates
                if preferred_block_id
                in {block.get("id") for block in _load(item.blocks_json, [])}
            ),
            None,
        )
    if not content:
        content = db.scalar(
            select(ContentVersion)
            .join(
                LearningContractVersion,
                LearningContractVersion.id
                == ContentVersion.learning_contract_version_id,
            )
            .where(
                ContentVersion.section_id == section.id,
                ContentVersion.publication_status == "published",
                LearningContractVersion.mission_version_id == mission_version_id,
            )
            .order_by(ContentVersion.version.desc())
        )
    if content and not quiz:
        quiz = db.scalar(
            select(QuizSet)
            .where(
                QuizSet.section_id == section.id,
                QuizSet.content_version_id == content.id,
                QuizSet.learning_contract_version_id
                == content.learning_contract_version_id,
                QuizSet.publication_status == "published",
            )
            .order_by(QuizSet.generation.asc())
        )
    if (
        not content
        or not quiz
        or content.publication_status != "published"
        or quiz.publication_status != "published"
        or quiz.section_id != section.id
        or quiz.content_version_id != content.id
        or not content.learning_contract_version_id
    ):
        raise AppError(
            "本节还没有完整且可冻结的正文题集",
            code="SECTION_CANDIDATE_INCOMPLETE",
            status=409,
        )
    contract = db.get(
        LearningContractVersion, content.learning_contract_version_id
    )
    if not contract or contract.mission_version_id != mission_version_id:
        raise AppError(
            "候选教材不属于当前采用的学习任务版本",
            code="SECTION_CANDIDATE_MISSION_MISMATCH",
            status=409,
        )
    binding = LearningRunSectionBinding(
        id=uid("run_section_binding"),
        learning_run_id=run.id,
        user_id=run.user_id,
        section_id=section.id,
        learning_contract_version_id=contract.id,
        content_version_id=content.id,
        initial_quiz_set_id=quiz.id,
        first_read_at=now(),
        source=source,
        source_fact_id=preferred_quiz_id or preferred_block_id or "",
        lineage_audit_json=_dump({
            "missionVersionId": mission_version_id,
            "contractVersionId": contract.id,
            "contentVersionId": content.id,
            "quizSetId": quiz.id,
        }),
    )
    db.add(binding)
    db.flush()
    return binding
