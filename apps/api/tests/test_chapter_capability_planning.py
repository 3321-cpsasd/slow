import json

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from app.ai.contracts import (
    GeneratedCapabilityMember,
    GeneratedCapabilityRelation,
    GeneratedCapabilitySubnetCandidate,
    GeneratedChapter,
    GeneratedConceptCandidate,
    GeneratedSectionOutline,
)
from app.core.errors import AppError
from app.infrastructure.tables import (
    AssessmentTarget,
    AssessmentTargetConceptBinding,
    Book,
    Base,
    CapabilityPlanningCandidate,
    CapabilityPlanningDecision,
    CapabilityRelationRequirement,
    Chapter,
    KnowledgeRelationCandidate,
    KnowledgeRelationIdentityDecision,
    LearningContractConcept,
    LearningMissionVersion,
    LearningPlan,
    Section,
    Series,
    Shelf,
    User,
)
from app.modules.curriculum.capability_planning import (
    freeze_chapter_capability_plans,
)
from app.modules.curriculum.chapter_planning import section_objectives_payload
from app.modules.learning.contracts import ensure_learning_contract


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return Session(engine)


def _seed_route(db: Session) -> tuple[Chapter, LearningMissionVersion]:
    db.add(User(id="user_cap_plan", name="能力规划学习者"))
    db.flush()
    db.add(
        Shelf(
            id="shelf_cap_plan",
            user_id="user_cap_plan",
            name="人工智能史",
            domain="computer-science",
        )
    )
    db.flush()
    db.add(
        LearningPlan(
            id="plan_cap_plan",
            shelf_id="shelf_cap_plan",
            topic="人工智能史",
            role="learner",
            experience="beginner",
            depth="deep",
            confidence="high",
        )
    )
    db.flush()
    mission = LearningMissionVersion(
        id="mission_cap_plan",
        plan_id="plan_cap_plan",
        user_id="user_cap_plan",
        version=1,
        status="active",
        why="理解人工智能发展脉络",
        target_capabilities_json="[]",
        constraints_json="{}",
        out_of_scope_json="[]",
        assumptions_json="[]",
        learner_context_json="{}",
        inferred_fields_json="[]",
        provenance_json="{}",
        schema_version="mission_v1",
        payload_hash="c" * 64,
    )
    db.add(mission)
    db.flush()
    db.add(
        Series(
            id="series_cap_plan",
            plan_id="plan_cap_plan",
            shelf_id="shelf_cap_plan",
            title="人工智能史",
            rationale="理解范式、繁荣与低谷。",
            initial_mission_version_id=mission.id,
        )
    )
    db.flush()
    db.add(
        Book(
            id="book_cap_plan",
            series_id="series_cap_plan",
            shelf_id="shelf_cap_plan",
            position=1,
            title="人工智能的形成与第一次低谷",
            topic="人工智能史",
            description="",
            estimated_minutes=180,
        )
    )
    db.flush()
    chapter = Chapter(
        id="chapter_cap_plan",
        book_id="book_cap_plan",
        position=1,
        title="从学科形成到第一次 AI 寒冬",
        objective="解释早期人工智能的发展关系",
    )
    db.add(chapter)
    db.flush()
    return chapter, mission


def _generated(*, disconnected: bool = False) -> GeneratedChapter:
    concept_specs = (
        (
            "dartmouth",
            "达特茅斯会议",
            "达特茅斯会议推动人工智能形成独立研究领域。",
        ),
        (
            "symbolism",
            "符号主义",
            "符号主义以符号表示和规则操作研究智能。",
        ),
        (
            "first-ai-winter",
            "第一次 AI 寒冬",
            "第一次 AI 寒冬是早期承诺与技术现实落差造成的低谷。",
        ),
    )
    sections = [
        GeneratedSectionOutline(
            title=label,
            question=f"{label}在早期人工智能发展中起什么作用？",
            objectives=[f"说明{label}及其在早期人工智能中的作用"],
            concept_candidate=GeneratedConceptCandidate(
                candidate_key=key,
                label=label,
                definition=definition,
                scope="人工智能学科形成、早期范式和第一次低谷",
            ),
            objective_dimensions=["mechanism"],
        )
        for key, label, definition in concept_specs
    ]
    relations = [
        GeneratedCapabilityRelation(
            from_section_position=1,
            to_section_position=2,
            relation_type="contributes_to",
            statement="达特茅斯会议推动的共同体使符号主义成为早期重要路径。",
        )
    ]
    if not disconnected:
        relations.append(
            GeneratedCapabilityRelation(
                from_section_position=2,
                to_section_position=3,
                relation_type="helps_explain",
                statement="符号主义的承诺与局限之间的落差有助于解释第一次 AI 寒冬。",
            )
        )
    return GeneratedChapter(
        sections=sections,
        capability_subnets=[
            GeneratedCapabilitySubnetCandidate(
                candidate_key="early-ai-rise-and-winter",
                label="解释 AI 学科形成、早期范式与第一次低谷之间的关系",
                operation="解释三个知识对象的关系并分析早期 AI 项目的瓶颈",
                boundary="限定在人工智能学科形成至第一次 AI 寒冬",
                members=[
                    GeneratedCapabilityMember(section_position=1, role="anchor"),
                    GeneratedCapabilityMember(section_position=2, role="required"),
                    GeneratedCapabilityMember(section_position=3, role="required"),
                ],
                relations=relations,
                assessment_section_position=3,
                assessment_objective_position=1,
                natural_stage_ceiling="gold",
            )
        ],
    )


def _persist_sections(
    db: Session, chapter: Chapter, generated: GeneratedChapter
) -> list[Section]:
    result = []
    for position, item in enumerate(generated.sections, start=1):
        section = Section(
            id=f"section_cap_plan_{position}",
            chapter_id=chapter.id,
            position=position,
            title=item.title,
            question=item.question,
            objectives_json=json.dumps(
                section_objectives_payload(item), ensure_ascii=False
            ),
        )
        db.add(section)
        result.append(section)
    db.flush()
    return result


def test_chapter_plan_freezes_composite_capability_and_contract_scope():
    with _session() as db:
        chapter, mission = _seed_route(db)
        generated = _generated()
        sections = _persist_sections(db, chapter, generated)

        freeze_chapter_capability_plans(
            db,
            series_id="series_cap_plan",
            chapter_id=chapter.id,
            sections=sections,
            generated_chapter=generated,
            published_allowlist=[],
        )
        contract = ensure_learning_contract(
            db,
            sections[2],
            mission_version_id=mission.id,
        )
        db.commit()

        planned = json.loads(sections[2].objectives_json)[0][
            "plannedCapability"
        ]
        target = db.scalar(
            select(AssessmentTarget).where(
                AssessmentTarget.capability_revision_id
                == planned["capabilityRevisionId"],
                AssessmentTarget.capability_stage_criterion_id
                == planned["stageCriterionId"],
            )
        )
        assert target is not None
        assert target.identity_status == "route_scoped_capability"
        assert (
            db.scalar(
                select(func.count())
                .select_from(AssessmentTargetConceptBinding)
                .where(
                    AssessmentTargetConceptBinding.assessment_target_id
                    == target.id
                )
            )
            == 3
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(LearningContractConcept)
                .where(LearningContractConcept.contract_version_id == contract.id)
            )
            == 3
        )
        assert db.scalar(
            select(func.count()).select_from(CapabilityPlanningCandidate)
        ) == 1
        assert db.scalar(
            select(func.count()).select_from(CapabilityPlanningDecision)
        ) == 1
        assert db.scalar(
            select(func.count()).select_from(KnowledgeRelationCandidate)
        ) == 2
        assert db.scalar(
            select(func.count()).select_from(KnowledgeRelationIdentityDecision)
        ) == 2
        assert db.scalar(
            select(func.count()).select_from(CapabilityRelationRequirement)
        ) == 2


def test_disconnected_required_knowledge_fails_chapter_planning_closed():
    with _session() as db:
        chapter, _mission = _seed_route(db)
        generated = _generated(disconnected=True)
        sections = _persist_sections(db, chapter, generated)

        with pytest.raises(AppError) as error:
            freeze_chapter_capability_plans(
                db,
                series_id="series_cap_plan",
                chapter_id=chapter.id,
                sections=sections,
                generated_chapter=generated,
                published_allowlist=[],
            )
        assert error.value.code == "CAPABILITY_SUBNET_REQUIRED_GRAPH_DISCONNECTED"


def test_same_relation_family_with_changed_meaning_is_unresolved():
    with _session() as db:
        chapter, _mission = _seed_route(db)
        generated = _generated()
        sections = _persist_sections(db, chapter, generated)
        freeze_chapter_capability_plans(
            db,
            series_id="series_cap_plan",
            chapter_id=chapter.id,
            sections=sections,
            generated_chapter=generated,
            published_allowlist=[],
        )
        changed_relation = generated.capability_subnets[0].relations[0].model_copy(
            update={"statement": "同一关系端点被改成了另一种含义。"}
        )
        changed_capability = generated.capability_subnets[0].model_copy(
            update={
                "relations": [
                    changed_relation,
                    generated.capability_subnets[0].relations[1],
                ]
            }
        )
        changed = generated.model_copy(
            update={"capability_subnets": [changed_capability]}
        )

        with pytest.raises(AppError) as error:
            freeze_chapter_capability_plans(
                db,
                series_id="series_cap_plan",
                chapter_id=chapter.id,
                sections=sections,
                generated_chapter=changed,
                published_allowlist=[],
            )
        assert error.value.code == "CAPABILITY_PLAN_RELATION_UNRESOLVED"
        assert db.scalar(
            select(func.count())
            .select_from(KnowledgeRelationCandidate)
            .where(KnowledgeRelationCandidate.status == "unresolved")
        ) == 1
        assert db.scalar(
            select(func.count())
            .select_from(KnowledgeRelationIdentityDecision)
            .where(KnowledgeRelationIdentityDecision.decision == "unresolved")
        ) == 1


def test_same_capability_key_with_changed_boundary_is_unresolved():
    with _session() as db:
        chapter, _mission = _seed_route(db)
        generated = _generated()
        sections = _persist_sections(db, chapter, generated)
        freeze_chapter_capability_plans(
            db,
            series_id="series_cap_plan",
            chapter_id=chapter.id,
            sections=sections,
            generated_chapter=generated,
            published_allowlist=[],
        )
        changed_capability = generated.capability_subnets[0].model_copy(
            update={"boundary": "改成覆盖第二次 AI 寒冬的不同能力范围"}
        )
        changed = generated.model_copy(
            update={"capability_subnets": [changed_capability]}
        )

        with pytest.raises(AppError) as error:
            freeze_chapter_capability_plans(
                db,
                series_id="series_cap_plan",
                chapter_id=chapter.id,
                sections=sections,
                generated_chapter=changed,
                published_allowlist=[],
            )
        assert error.value.code == "CAPABILITY_PLAN_IDENTITY_UNRESOLVED"
