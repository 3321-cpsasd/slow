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
from .capabilities import (
    bind_assessment_target_to_capability_subnet,
    ensure_ask_me_stage_targets,
    ensure_route_capability,
)


M1_NAMESPACE = "m1_provisional"
ROUTE_KNOWLEDGE_NAMESPACE = "route_knowledge"
ROUTE_KNOWLEDGE_IDENTITY_STATUS = "route_scoped_knowledge"
RANK_SETTLEABLE_IDENTITY_STATUSES = {
    "published_knowledge_graph",
    ROUTE_KNOWLEDGE_IDENTITY_STATUS,
}
M1_CONTRACT_SCHEMA_VERSION = "learning_contract_v2_capability_stages"


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


def _section_objectives(
    section: Section,
) -> list[tuple[str, bool, str | None, dict | None]]:
    parsed: list[tuple[str, bool | None, str | None, dict | None]] = []
    for item in _load(section.objectives_json, []):
        if isinstance(item, dict):
            statement = str(item.get("statement") or item.get("objective") or "").strip()
            required = item.get("required", item.get("core"))
            dimension = str(item.get("dimension") or "").strip() or None
            candidate = item.get("conceptCandidate")
            if not isinstance(candidate, dict):
                candidate = None
        else:
            statement = str(item).strip()
            required = None
            dimension = None
            candidate = None
        if statement:
            parsed.append(
                (
                    statement,
                    bool(required) if required is not None else None,
                    dimension,
                    candidate,
                )
            )
    if not parsed:
        parsed = [(section.question.strip(), True, None, None)]

    result: list[tuple[str, bool, str | None, dict | None]] = []
    positions: dict[str, int] = {}
    for position, (statement, explicit_required, dimension, candidate) in enumerate(parsed):
        key = _objective_key(statement)
        required = explicit_required if explicit_required is not None else position == 0
        if key in positions:
            old_statement, old_required, old_dimension, old_candidate = result[
                positions[key]
            ]
            if old_dimension != dimension or old_candidate != candidate:
                raise AppError(
                    "同一能力目标声明了冲突的知识身份或维度",
                    code="SECTION_OBJECTIVE_IDENTITY_CONFLICT",
                    status=409,
                )
            result[positions[key]] = (
                old_statement,
                old_required or required,
                old_dimension,
                old_candidate,
            )
        else:
            positions[key] = len(result)
            result.append((statement, required, dimension, candidate))
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


def _series_id_for_section(db: Session, section: Section) -> str:
    series_id = db.scalar(
        select(Book.series_id)
        .join(Chapter, Chapter.book_id == Book.id)
        .where(Chapter.id == section.chapter_id)
    )
    if not series_id:
        raise AppError(
            "小节缺少所属系列，不能建立稳定的能力身份",
            code="SECTION_SERIES_MISSING",
            status=500,
        )
    return series_id


def _route_rank_policy(statement: str) -> dict:
    """Return the complete local capability ladder frozen with a route node."""

    return {
        "version": "knowledge_rank_policy_v1",
        "capabilityScope": statement,
        "rankCeiling": "master",
        "dimensionRanks": {
            "recognition": "bronze",
            "mechanism": "silver",
            "application": "gold",
            "boundary": "platinum",
            "transfer": "diamond",
        },
    }


def materialize_route_target(
    db: Session,
    *,
    series_id: str,
    statement: str,
) -> AssessmentTarget:
    """Create a stable, rank-settleable identity without cross-route guessing."""

    objective_hash = _objective_key(statement)
    namespace = f"{ROUTE_KNOWLEDGE_NAMESPACE}:{series_id}"
    semantic_key = f"route:{series_id}:{objective_hash}"
    concept_id = _stable_id("concept_route", series_id, objective_hash)
    revision_id = _stable_id("concept_revision_route", concept_id, 1)
    objective_id = _stable_id("learning_objective_route", series_id, objective_hash)
    target_id = _stable_id(
        "target_route", series_id, objective_hash, "recognition", "standard"
    )

    concept = db.get(Concept, concept_id)
    if concept is None:
        db.add(
            Concept(
                id=concept_id,
                namespace=namespace,
                concept_key=objective_hash,
                canonical_name=statement,
                status="active",
                origin="route_scoped",
            )
        )
    revision = db.get(ConceptRevision, revision_id)
    if revision is None:
        db.add(
            ConceptRevision(
                id=revision_id,
                concept_id=concept_id,
                revision=1,
                label=statement,
                definition=statement,
                scope_json=_dump(
                    {
                        "routeScope": {"seriesId": series_id},
                        "rankPolicy": _route_rank_policy(statement),
                    }
                ),
                boundaries_json="[]",
                provenance_mode="route_scoped",
                verification_status="route_scoped",
            )
        )
    objective = db.get(LearningObjective, objective_id)
    if objective is None:
        db.add(
            LearningObjective(
                id=objective_id,
                namespace=namespace,
                objective_key=objective_hash,
                statement=statement,
                cognitive_verb="demonstrate",
                outcome_type="knowledge",
                provenance_mode="route_scoped",
                verification_status="route_scoped",
                status="active",
            )
        )
    db.flush()
    target = db.get(AssessmentTarget, target_id)
    capability_revision, bronze_criterion = ensure_route_capability(
        db,
        series_id=series_id,
        concept_revision_id=revision_id,
    )
    if target is None:
        target = AssessmentTarget(
            id=target_id,
            concept_revision_id=revision_id,
            learning_objective_id=objective_id,
            capability_revision_id=capability_revision.id,
            capability_stage_criterion_id=bronze_criterion.id,
            objective_key=semantic_key,
            objective_statement=statement,
            dimension="recognition",
            target_depth="standard",
            identity_status=ROUTE_KNOWLEDGE_IDENTITY_STATUS,
            status="active",
        )
        db.add(target)
    elif (
        target.capability_revision_id is None
        and target.capability_stage_criterion_id is None
    ):
        target.capability_revision_id = capability_revision.id
        target.capability_stage_criterion_id = bronze_criterion.id
    db.flush()
    bind_assessment_target_to_capability_subnet(
        db,
        assessment_target_id=target.id,
        capability_revision_id=capability_revision.id,
        stage_criterion_id=bronze_criterion.id,
    )
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

    # Stable M2 identities must be declared by exact baseline keys in the
    # section objective metadata. Never infer them from title/prose similarity.
    from ..knowledge.fact_graph import resolve_published_section_identities

    published_identities = resolve_published_section_identities(
        db,
        chapter_id=section.chapter_id,
        objectives_json=section.objectives_json,
    )
    if published_identities:
        series_id = _series_id_for_section(db, section)
        expected_pairs = {
            (item.concept_revision_id, item.learning_objective_id)
            for item in published_identities
        }
        if rows:
            actual_pairs = {
                (target.concept_revision_id, target.learning_objective_id)
                for _, target in rows
            }
            if actual_pairs != expected_pairs or any(
                target.identity_status != "published_knowledge_graph"
                for _, target in rows
            ):
                raise AppError(
                    "小节已有考核目标与显式发布知识身份不一致，不能静默改写",
                    code="SECTION_TARGET_STABLE_IDENTITY_CONFLICT",
                    status=409,
                )
            return sorted(rows, key=lambda pair: pair[0].position)
        stable_rows = []
        for position, identity in enumerate(published_identities, 1):
            semantic_key = (
                f"{identity.objective_key}@{identity.concept_revision_id}"
            )
            dimension = (
                "application"
                if "code" in identity.verification_policy
                else "recognition"
            )
            target = db.scalar(
                select(AssessmentTarget).where(
                    AssessmentTarget.objective_key == semantic_key,
                    AssessmentTarget.dimension == dimension,
                    AssessmentTarget.target_depth == "standard",
                )
            )
            capability_revision, bronze_criterion = ensure_route_capability(
                db,
                series_id=series_id,
                concept_revision_id=identity.concept_revision_id,
            )
            if target is None:
                target = AssessmentTarget(
                    id=_stable_id(
                        "target_knowledge_graph",
                        identity.concept_revision_id,
                        identity.learning_objective_id,
                        dimension,
                        "standard",
                    ),
                    concept_revision_id=identity.concept_revision_id,
                    learning_objective_id=identity.learning_objective_id,
                    capability_revision_id=capability_revision.id,
                    capability_stage_criterion_id=bronze_criterion.id,
                    objective_key=semantic_key,
                    objective_statement=identity.objective_statement,
                    dimension=dimension,
                    target_depth="standard",
                    identity_status="published_knowledge_graph",
                    status="active",
                )
                db.add(target)
                db.flush()
            elif (
                target.capability_revision_id is None
                and target.capability_stage_criterion_id is None
            ):
                target.capability_revision_id = capability_revision.id
                target.capability_stage_criterion_id = bronze_criterion.id
                db.flush()
            bind_assessment_target_to_capability_subnet(
                db,
                assessment_target_id=target.id,
                capability_revision_id=capability_revision.id,
                stage_criterion_id=bronze_criterion.id,
            )
            binding = SectionAssessmentTarget(
                id=_stable_id("section_target_knowledge_graph", section.id, target.id),
                section_id=section.id,
                assessment_target_id=target.id,
                position=position,
                required=True,
                verification_policy=identity.verification_policy,
            )
            db.add(binding)
            db.flush()
            stable_rows.append((binding, target))
        return stable_rows

    # Existing bindings are historical server-owned semantics. Never rewrite or
    # expand a partially materialized M1 set during migration.
    objectives = [] if rows else _section_objectives(section)
    series_id = _series_id_for_section(db, section) if objectives else None
    for position, (statement, required, dimension, candidate) in enumerate(
        objectives, 1
    ):
        key = _objective_key(statement)
        pair = by_key.get(key)
        if pair is None:
            if candidate is not None:
                from ..knowledge.identity import materialize_candidate_target

                target = materialize_candidate_target(
                    db,
                    series_id=series_id,
                    section_id=section.id,
                    statement=statement,
                    dimension=dimension or "recognition",
                    candidate=candidate,
                )
                binding_prefix = "section_target_candidate"
            else:
                target = materialize_route_target(
                    db,
                    series_id=series_id,
                    statement=statement,
                )
                binding_prefix = "section_target_route"
            binding = SectionAssessmentTarget(
                id=_stable_id(binding_prefix, section.id, target.id),
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
        if not target.concept_revision_id or not target.learning_objective_id:
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
    diagnostic_targets: list[tuple[AssessmentTarget, str]] = []
    primary_capability_target = next(
        (
            target
            for binding, target in target_rows
            if binding.required
            and target.capability_revision_id
            and target.concept_revision_id
        ),
        next(
            (
                target
                for _binding, target in target_rows
                if target.capability_revision_id and target.concept_revision_id
            ),
            None,
        ),
    )
    if primary_capability_target is not None:
        ask_me_targets = ensure_ask_me_stage_targets(
            db,
            series_id=_series_id_for_section(db, section),
            capability_revision_id=(
                primary_capability_target.capability_revision_id
            ),
            concept_revision_id=primary_capability_target.concept_revision_id,
        )
        diagnostic_targets = [
            (ask_me_targets["mechanism"], "oral_explanation_v1"),
            (ask_me_targets["boundary"], "oral_boundary_v1"),
            (ask_me_targets["transfer"], "oral_transfer_probe_v1"),
            (ask_me_targets["application"], "standard_application_v1"),
        ]
    uses_rank_settleable_knowledge = bool(target_rows) and all(
        target.identity_status in RANK_SETTLEABLE_IDENTITY_STATUSES
        for _, target in target_rows
    )
    identity_statuses = {target.identity_status for _, target in target_rows}
    contract_provenance = (
        "published_knowledge_graph"
        if identity_statuses == {"published_knowledge_graph"}
        else "route_scoped_knowledge"
        if identity_statuses == {ROUTE_KNOWLEDGE_IDENTITY_STATUS}
        else provenance_mode
    )
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
        "diagnosticTargets": [
            {
                "assessmentTargetId": target.id,
                "conceptRevisionId": target.concept_revision_id,
                "learningObjectiveId": target.learning_objective_id,
                "capabilityRevisionId": target.capability_revision_id,
                "capabilityStageCriterionId": (
                    target.capability_stage_criterion_id
                ),
                "position": len(target_rows) + position,
                "verificationPolicy": verification_policy,
            }
            for position, (target, verification_policy) in enumerate(
                diagnostic_targets, 1
            )
        ],
        "provenanceMode": contract_provenance,
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
                "mode": contract_provenance,
                "sourceObjectives": _load(section.objectives_json, []),
                "targetDepth": delivery_depth,
                "contextPolicyVersion": "lesson_content_context_v1",
                "conceptRevisionIds": [
                    target.concept_revision_id for _, target in target_rows
                ],
                "learningObjectiveIds": [
                    target.learning_objective_id for _, target in target_rows
                ],
            }
        ),
        provenance_mode=contract_provenance,
        lineage_status=("verified" if uses_rank_settleable_knowledge else "provisional"),
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
    for position, (target, verification_policy) in enumerate(
        diagnostic_targets,
        len(target_rows) + 1,
    ):
        if target.learning_objective_id not in seen_objectives:
            seen_objectives.add(target.learning_objective_id)
            db.add(
                LearningContractObjective(
                    id=_stable_id(
                        "contract_objective_capability",
                        contract.id,
                        target.learning_objective_id,
                    ),
                    contract_version_id=contract.id,
                    learning_objective_id=target.learning_objective_id,
                    position=len(seen_objectives),
                    role="diagnostic",
                )
            )
        db.add(
            LearningContractAssessmentTarget(
                id=_stable_id(
                    "contract_target_capability", contract.id, target.id
                ),
                contract_version_id=contract.id,
                assessment_target_id=target.id,
                position=position,
                required=False,
                verification_policy=verification_policy,
                evidence_policy="capability_evidence_v1",
                diagnostic_only=True,
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


def require_rank_settleable_contract(
    db: Session,
    contract: LearningContractVersion,
) -> None:
    """Fail publication unless every formal target can produce a rank receipt."""

    from .knowledge_ranks import (
        RANK_SETTLEABLE_REVISION_STATUSES,
        rank_policy_for_revision,
    )

    rows = db.execute(
        select(LearningContractAssessmentTarget, AssessmentTarget)
        .join(
            AssessmentTarget,
            AssessmentTarget.id
            == LearningContractAssessmentTarget.assessment_target_id,
        )
        .where(
            LearningContractAssessmentTarget.contract_version_id == contract.id,
            LearningContractAssessmentTarget.diagnostic_only.is_(False),
        )
        .order_by(LearningContractAssessmentTarget.position)
    ).all()
    if not rows:
        raise AppError(
            "本节还没有可结算的能力目标，请重新规划后再生成",
            code="CONTRACT_RANK_TARGET_MISSING",
            status=409,
        )

    for _binding, target in rows:
        if (
            target.identity_status not in RANK_SETTLEABLE_IDENTITY_STATUSES
            or not target.concept_revision_id
            or not target.learning_objective_id
        ):
            raise AppError(
                "本节能力目标尚未取得正式段位身份，请重新规划后再生成",
                code="CONTRACT_RANK_IDENTITY_UNSETTLEABLE",
                status=409,
            )
        revision = db.get(ConceptRevision, target.concept_revision_id)
        policy = rank_policy_for_revision(revision) if revision else None
        if (
            revision is None
            or revision.verification_status not in RANK_SETTLEABLE_REVISION_STATUSES
            or policy is None
            or target.dimension not in policy["dimensionRanks"]
        ):
            raise AppError(
                "本节能力目标缺少完整段位规则，已停止发布",
                code="CONTRACT_RANK_POLICY_INVALID",
                status=409,
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
    from .knowledge_ranks import require_effective_rank_targets

    quiz_target_ids = {
        str(question.get("assessmentTargetId") or "").strip()
        for question in _load(quiz.questions_json, [])
        if isinstance(question, dict)
        and str(question.get("assessmentTargetId") or "").strip()
    }
    require_effective_rank_targets(
        db,
        learning_contract_version_id=contract.id,
        target_ids=quiz_target_ids,
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
