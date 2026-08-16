import json

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.infrastructure.tables import (
    Base,
    AssessmentTarget,
    AssessmentTargetConceptBinding,
    AssessmentTargetRelationBinding,
    CapabilityConceptBinding,
    CapabilityRelationRequirement,
    CapabilityRevision,
    CapabilitySubnet,
    Concept,
    ConceptRevision,
    KnowledgeNetworkConceptBinding,
    KnowledgeNetworkRelationBinding,
    KnowledgeNetworkRevision,
    KnowledgeRelationRevision,
    LearningPlan,
    Series,
    Shelf,
    User,
)
from app.modules.learning.capabilities import (
    CapabilityConceptSpec,
    CapabilityRelationSpec,
    bind_assessment_target_to_capability_subnet,
    ensure_ask_me_stage_targets,
    ensure_route_capability_subnet,
    validate_capability_subnet,
)


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return Session(engine)


def _add_concepts(db: Session) -> None:
    db.add(User(id="user_ai_history", name="AI 历史学习者"))
    db.flush()
    db.add(
        Shelf(
            id="shelf_ai_history",
            user_id="user_ai_history",
            name="人工智能史",
            domain="computer-science",
        )
    )
    db.flush()
    db.add(
        LearningPlan(
            id="plan_ai_history",
            shelf_id="shelf_ai_history",
            topic="人工智能史",
            role="learner",
            experience="beginner",
            depth="standard",
            confidence="high",
        )
    )
    db.flush()
    db.add(
        Series(
            id="series_ai_history",
            plan_id="plan_ai_history",
            shelf_id="shelf_ai_history",
            title="人工智能史",
            rationale="理解人工智能发展中的范式、繁荣与低谷。",
        )
    )
    db.flush()
    specs = (
        (
            "symbolism",
            "符号主义",
            "以符号表示和规则操作研究智能的人工智能路径。",
        ),
        (
            "dartmouth",
            "达特茅斯会议",
            "1956 年汇聚研究者并推动人工智能形成独立研究领域的会议。",
        ),
        (
            "first_ai_winter",
            "第一次 AI 寒冬",
            "早期承诺与技术现实落差造成投入和关注下降的时期。",
        ),
    )
    for key, label, definition in specs:
        concept_id = f"concept_{key}"
        db.add(
            Concept(
                id=concept_id,
                namespace="ai_history",
                concept_key=key,
                canonical_name=label,
                status="active",
                origin="test",
            )
        )
        db.add(
            ConceptRevision(
                id=f"revision_{key}",
                concept_id=concept_id,
                revision=1,
                label=label,
                definition=definition,
                scope_json=json.dumps({"domain": "ai_history"}),
                boundaries_json="[]",
                provenance_mode="test",
                verification_status="reviewed",
            )
        )
    db.flush()


def _concept_specs() -> tuple[CapabilityConceptSpec, ...]:
    return (
        CapabilityConceptSpec("revision_dartmouth", "anchor", True),
        CapabilityConceptSpec("revision_symbolism", "required", True),
        CapabilityConceptSpec("revision_first_ai_winter", "required", True),
    )


def _relation_specs() -> tuple[CapabilityRelationSpec, ...]:
    return (
        CapabilityRelationSpec(
            "revision_dartmouth",
            "revision_symbolism",
            "contributes_to",
            "达特茅斯会议推动的研究共同体让符号主义成为早期重要路径。",
            minimum_stage="silver",
            purpose="explain",
        ),
        CapabilityRelationSpec(
            "revision_symbolism",
            "revision_first_ai_winter",
            "helps_explain",
            "符号主义的早期承诺与技术局限之间的落差有助于解释第一次 AI 寒冬。",
            minimum_stage="silver",
            purpose="causal_explanation",
        ),
    )


def test_three_concepts_and_required_relations_freeze_one_capability_subnet():
    with _session() as db:
        _add_concepts(db)
        capability, bronze = ensure_route_capability_subnet(
            db,
            series_id="series_ai_history",
            label="解释 AI 学科形成、早期研究范式与第一次低谷之间的关系",
            concepts=_concept_specs(),
            relations=_relation_specs(),
            boundary={"period": "1956-1974"},
            context={"domain": "ai_history"},
        )
        db.commit()

        assert bronze.stage == "bronze"
        assert db.scalar(select(func.count()).select_from(CapabilityRevision)) == 1
        assert db.scalar(select(func.count()).select_from(CapabilitySubnet)) == 1
        assert (
            db.scalar(select(func.count()).select_from(CapabilityConceptBinding))
            == 3
        )
        assert (
            db.scalar(
                select(func.count()).select_from(CapabilityRelationRequirement)
            )
            == 2
        )
        subnet = validate_capability_subnet(
            db, capability_revision_id=capability.id
        )
        assert subnet.status == "frozen"
        assert (
            db.scalar(
                select(func.count()).select_from(KnowledgeNetworkConceptBinding)
            )
            == 3
        )
        assert (
            db.scalar(
                select(func.count()).select_from(KnowledgeNetworkRelationBinding)
            )
            == 2
        )
        assert (
            db.scalar(select(func.count()).select_from(KnowledgeRelationRevision))
            == 2
        )


def test_missing_required_relation_fails_closed_for_composite_capability():
    with _session() as db:
        _add_concepts(db)
        with pytest.raises(AppError) as error:
            ensure_route_capability_subnet(
                db,
                series_id="series_ai_history",
                label="解释 AI 学科形成、早期研究范式与第一次低谷之间的关系",
                concepts=_concept_specs(),
                relations=(_relation_specs()[0],),
            )
        assert error.value.code == "CAPABILITY_SUBNET_REQUIRED_GRAPH_DISCONNECTED"


def test_frozen_subnet_detects_removed_relation_requirement():
    with _session() as db:
        _add_concepts(db)
        capability, _bronze = ensure_route_capability_subnet(
            db,
            series_id="series_ai_history",
            label="解释 AI 学科形成、早期研究范式与第一次低谷之间的关系",
            concepts=_concept_specs(),
            relations=_relation_specs(),
        )
        removed = db.scalar(
            select(CapabilityRelationRequirement).where(
                CapabilityRelationRequirement.capability_revision_id
                == capability.id,
                CapabilityRelationRequirement.position == 2,
            )
        )
        assert removed is not None
        db.delete(removed)
        db.flush()

        with pytest.raises(AppError) as error:
            validate_capability_subnet(db, capability_revision_id=capability.id)
        assert error.value.code == "CAPABILITY_SUBNET_REQUIRED_GRAPH_DISCONNECTED"


def test_frozen_subnet_detects_tampered_relation_meaning():
    with _session() as db:
        _add_concepts(db)
        capability, _bronze = ensure_route_capability_subnet(
            db,
            series_id="series_ai_history",
            label="解释 AI 学科形成、早期研究范式与第一次低谷之间的关系",
            concepts=_concept_specs(),
            relations=_relation_specs(),
        )
        relation = db.scalar(
            select(KnowledgeRelationRevision).order_by(
                KnowledgeRelationRevision.id
            )
        )
        assert relation is not None
        relation.statement = "被静默改写的关系"
        db.flush()

        with pytest.raises(AppError) as error:
            validate_capability_subnet(db, capability_revision_id=capability.id)
        assert error.value.code == "KNOWLEDGE_RELATION_HASH_MISMATCH"


def test_supporting_knowledge_cannot_become_required_silently():
    with _session() as db:
        _add_concepts(db)
        with pytest.raises(AppError) as error:
            ensure_route_capability_subnet(
                db,
                series_id="series_ai_history",
                label="解释 AI 早期发展",
                concepts=(
                    CapabilityConceptSpec("revision_dartmouth", "anchor", True),
                    CapabilityConceptSpec("revision_symbolism", "supporting", True),
                ),
                relations=(),
            )
        assert error.value.code == "CAPABILITY_SUBNET_SUPPORTING_REQUIRED"


def test_identical_subnet_request_is_idempotent():
    with _session() as db:
        _add_concepts(db)
        first, _ = ensure_route_capability_subnet(
            db,
            series_id="series_ai_history",
            label="解释 AI 学科形成、早期研究范式与第一次低谷之间的关系",
            concepts=_concept_specs(),
            relations=_relation_specs(),
        )
        second, _ = ensure_route_capability_subnet(
            db,
            series_id="series_ai_history",
            label="解释 AI 学科形成、早期研究范式与第一次低谷之间的关系",
            concepts=_concept_specs(),
            relations=_relation_specs(),
        )
        db.commit()

        assert first.id == second.id
        assert (
            db.scalar(select(func.count()).select_from(KnowledgeNetworkRevision))
            == 1
        )
        assert db.scalar(select(func.count()).select_from(CapabilitySubnet)) == 1


def test_stage_targets_bind_multiple_nodes_and_only_stage_relevant_relations():
    with _session() as db:
        _add_concepts(db)
        capability, bronze = ensure_route_capability_subnet(
            db,
            series_id="series_ai_history",
            label="解释 AI 学科形成、早期研究范式与第一次低谷之间的关系",
            concepts=_concept_specs(),
            relations=_relation_specs(),
        )
        bronze_target = AssessmentTarget(
            id="target_ai_history_bronze",
            concept_revision_id="revision_dartmouth",
            capability_revision_id=capability.id,
            capability_stage_criterion_id=bronze.id,
            objective_key="ai_history_bronze",
            objective_statement=bronze.statement,
            dimension="recognition",
            target_depth="standard",
            identity_status="route_scoped_capability",
            status="active",
        )
        db.add(bronze_target)
        db.flush()
        bind_assessment_target_to_capability_subnet(
            db,
            assessment_target_id=bronze_target.id,
            capability_revision_id=capability.id,
            stage_criterion_id=bronze.id,
        )
        ask_me = ensure_ask_me_stage_targets(
            db,
            series_id="series_ai_history",
            capability_revision_id=capability.id,
            concept_revision_id="revision_dartmouth",
        )
        db.commit()

        bronze_nodes = db.scalar(
            select(func.count())
            .select_from(AssessmentTargetConceptBinding)
            .where(
                AssessmentTargetConceptBinding.assessment_target_id
                == bronze_target.id
            )
        )
        bronze_relations = db.scalar(
            select(func.count())
            .select_from(AssessmentTargetRelationBinding)
            .where(
                AssessmentTargetRelationBinding.assessment_target_id
                == bronze_target.id
            )
        )
        silver_relations = db.scalar(
            select(func.count())
            .select_from(AssessmentTargetRelationBinding)
            .where(
                AssessmentTargetRelationBinding.assessment_target_id
                == ask_me["mechanism"].id
            )
        )
        assert bronze_nodes == 3
        assert bronze_relations == 0
        assert silver_relations == 2
