import json

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.infrastructure.tables import (
    AssessmentTarget,
    Book,
    Base,
    CapabilityPlanningCandidate,
    CapabilityRevision,
    Chapter,
    ConceptRevision,
    IdentityPublicationDecision,
    KnowledgeIdentityCandidate,
    KnowledgeRelationRevision,
    LearningMissionVersion,
    LearningPlan,
    PublishedCapabilityIdentity,
    PublishedConceptIdentity,
    PublishedRelationIdentity,
    Section,
    Series,
    Shelf,
    User,
)
from app.modules.curriculum.capability_planning import (
    freeze_chapter_capability_plans,
)
from app.modules.curriculum.chapter_planning import section_objectives_payload
from app.modules.knowledge.identity import resolve_candidate_revision
from app.modules.knowledge.publication import (
    publish_capability_candidate,
    publish_concept_candidate,
)
from app.modules.learning.contracts import ensure_learning_contract
from test_chapter_capability_planning import _generated


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return Session(engine)


def _seed_route(
    db: Session, suffix: str
) -> tuple[Chapter, LearningMissionVersion]:
    user_id = f"user_publication_{suffix}"
    shelf_id = f"shelf_publication_{suffix}"
    plan_id = f"plan_publication_{suffix}"
    mission_id = f"mission_publication_{suffix}"
    series_id = f"series_publication_{suffix}"
    db.add(User(id=user_id, name=f"审核学习者 {suffix}"))
    db.flush()
    db.add(
        Shelf(
            id=shelf_id,
            user_id=user_id,
            name="人工智能史",
            domain="computer-science",
        )
    )
    db.flush()
    db.add(
        LearningPlan(
            id=plan_id,
            shelf_id=shelf_id,
            topic="人工智能史",
            role="learner",
            experience="beginner",
            depth="deep",
            confidence="high",
        )
    )
    db.flush()
    mission = LearningMissionVersion(
        id=mission_id,
        plan_id=plan_id,
        user_id=user_id,
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
        payload_hash=(suffix[0] * 64),
    )
    db.add(mission)
    db.flush()
    db.add(
        Series(
            id=series_id,
            plan_id=plan_id,
            shelf_id=shelf_id,
            title="人工智能史",
            rationale="理解范式、繁荣与低谷。",
            initial_mission_version_id=mission.id,
        )
    )
    db.flush()
    db.add(
        Book(
            id=f"book_publication_{suffix}",
            series_id=series_id,
            shelf_id=shelf_id,
            position=1,
            title="人工智能的形成与第一次低谷",
            topic="人工智能史",
            description="",
            estimated_minutes=180,
        )
    )
    db.flush()
    chapter = Chapter(
        id=f"chapter_publication_{suffix}",
        book_id=f"book_publication_{suffix}",
        position=1,
        title="从学科形成到第一次 AI 寒冬",
        objective="解释早期人工智能的发展关系",
    )
    db.add(chapter)
    db.flush()
    return chapter, mission


def _persist_sections(db: Session, chapter: Chapter, generated) -> list[Section]:
    sections = []
    for position, item in enumerate(generated.sections, start=1):
        section = Section(
            id=f"section_{chapter.id}_{position}",
            chapter_id=chapter.id,
            position=position,
            title=item.title,
            question=item.question,
            objectives_json=json.dumps(
                section_objectives_payload(item), ensure_ascii=False
            ),
        )
        db.add(section)
        sections.append(section)
    db.flush()
    return sections


def _plan(db: Session, suffix: str, generated=None):
    chapter, mission = _seed_route(db, suffix)
    generated = generated or _generated()
    sections = _persist_sections(db, chapter, generated)
    freeze_chapter_capability_plans(
        db,
        series_id=f"series_publication_{suffix}",
        chapter_id=chapter.id,
        sections=sections,
        generated_chapter=generated,
        published_allowlist=[],
    )
    candidate = db.scalar(
        select(CapabilityPlanningCandidate).where(
            CapabilityPlanningCandidate.chapter_id == chapter.id
        )
    )
    return chapter, mission, sections, candidate


def _publish_first(db: Session):
    _chapter, _mission, _sections, candidate = _plan(db, "a")
    result = publish_capability_candidate(
        db,
        planning_candidate_id=candidate.id,
        reviewer_id="reviewer-1",
        review={"outcome": "approved", "basis": "人工审核语义与边界"},
    )
    db.flush()
    return candidate, result


def test_published_capability_is_reused_across_series_and_contract_is_published():
    with _session() as db:
        _candidate_a, published = _publish_first(db)
        assert db.scalar(
            select(func.count()).select_from(PublishedConceptIdentity)
        ) == 3
        assert db.scalar(
            select(func.count()).select_from(PublishedRelationIdentity)
        ) == 2
        assert db.scalar(
            select(func.count()).select_from(PublishedCapabilityIdentity)
        ) == 1

        _chapter_b, mission_b, sections_b, candidate_b = _plan(db, "b")
        planned = json.loads(sections_b[2].objectives_json)[0][
            "plannedCapability"
        ]
        assert planned["capabilityRevisionId"] == published.capability_revision_id
        contract = ensure_learning_contract(
            db,
            sections_b[2],
            mission_version_id=mission_b.id,
        )
        target = db.scalar(
            select(AssessmentTarget).where(
                AssessmentTarget.capability_revision_id
                == published.capability_revision_id,
                AssessmentTarget.identity_status == "published_capability",
            )
        )
        assert target is not None
        assert contract.provenance_mode == "published_knowledge_graph"
        assert candidate_b.status == "resolved"
        assert db.scalar(
            select(func.count())
            .select_from(IdentityPublicationDecision)
            .where(IdentityPublicationDecision.decision == "publish_new_identity")
        ) == 6


def test_changed_relation_requires_explicit_new_version_then_retry_reuses_it():
    with _session() as db:
        _candidate_a, first = _publish_first(db)
        generated = _generated()
        changed_relation = generated.capability_subnets[0].relations[0].model_copy(
            update={"statement": "达特茅斯会议通过另一条经审核的关系影响符号主义。"}
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
        chapter_b, _mission_b = _seed_route(db, "b")
        sections_b = _persist_sections(db, chapter_b, changed)
        with pytest.raises(AppError) as conflict:
            freeze_chapter_capability_plans(
                db,
                series_id="series_publication_b",
                chapter_id=chapter_b.id,
                sections=sections_b,
                generated_chapter=changed,
                published_allowlist=[],
            )
        assert conflict.value.code == "CAPABILITY_PLAN_RELATION_UNRESOLVED"
        candidate_b = db.scalar(
            select(CapabilityPlanningCandidate).where(
                CapabilityPlanningCandidate.chapter_id == chapter_b.id
            )
        )
        prior_capability = db.scalar(select(PublishedCapabilityIdentity))
        prior_relations = db.scalars(select(PublishedRelationIdentity)).all()
        changed_family = next(
            item
            for item in prior_relations
            if item.family_key.endswith(":contributes_to:" + item.family_key.split(":")[-1])
        )
        published_v2 = publish_capability_candidate(
            db,
            planning_candidate_id=candidate_b.id,
            reviewer_id="reviewer-2",
            review={"outcome": "approved", "basis": "确认关系语义形成新版本"},
            relation_supersedes={changed_family.family_key: changed_family.id},
            capability_supersedes_publication_id=prior_capability.id,
        )
        assert published_v2.decision == "publish_new_revision"
        second_capability = db.scalar(
            select(PublishedCapabilityIdentity).where(
                PublishedCapabilityIdentity.capability_revision_id
                == published_v2.capability_revision_id
            )
        )
        assert second_capability.supersedes_id == prior_capability.id
        first_revision = db.get(
            CapabilityRevision, first.capability_revision_id
        )
        second_revision = db.get(
            CapabilityRevision, published_v2.capability_revision_id
        )
        assert second_revision.capability_id == first_revision.capability_id
        assert second_revision.revision == first_revision.revision + 1
        changed_relation_publication = db.scalar(
            select(PublishedRelationIdentity).where(
                PublishedRelationIdentity.supersedes_id == changed_family.id
            )
        )
        first_relation_revision = db.get(
            KnowledgeRelationRevision,
            changed_family.knowledge_relation_revision_id,
        )
        second_relation_revision = db.get(
            KnowledgeRelationRevision,
            changed_relation_publication.knowledge_relation_revision_id,
        )
        assert (
            second_relation_revision.knowledge_relation_id
            == first_relation_revision.knowledge_relation_id
        )
        assert second_relation_revision.revision == first_relation_revision.revision + 1

        freeze_chapter_capability_plans(
            db,
            series_id="series_publication_b",
            chapter_id=chapter_b.id,
            sections=sections_b,
            generated_chapter=changed,
            published_allowlist=[],
        )
        planned = json.loads(sections_b[2].objectives_json)[0][
            "plannedCapability"
        ]
        assert planned["capabilityRevisionId"] == published_v2.capability_revision_id
        assert published_v2.capability_revision_id != first.capability_revision_id


def test_changed_concept_stays_unresolved_until_reviewed_supersedes_decision():
    with _session() as db:
        _publish_first(db)
        generated = _generated()
        changed_concept = generated.sections[0].concept_candidate.model_copy(
            update={
                "definition": "达特茅斯会议的新版本定义明确区分会议事件与后续共同体。"
            }
        )
        changed_section = generated.sections[0].model_copy(
            update={"concept_candidate": changed_concept}
        )
        changed = generated.model_copy(
            update={"sections": [changed_section, *generated.sections[1:]]}
        )
        chapter_b, _mission_b = _seed_route(db, "b")
        sections_b = _persist_sections(db, chapter_b, changed)
        with pytest.raises(AppError) as conflict:
            freeze_chapter_capability_plans(
                db,
                series_id="series_publication_b",
                chapter_id=chapter_b.id,
                sections=sections_b,
                generated_chapter=changed,
                published_allowlist=[],
            )
        assert conflict.value.code == "CAPABILITY_PLAN_CONCEPT_UNRESOLVED"
        unresolved = db.scalar(
            select(KnowledgeIdentityCandidate).where(
                KnowledgeIdentityCandidate.series_id == "series_publication_b",
                KnowledgeIdentityCandidate.candidate_key == "dartmouth",
            )
        )
        prior = db.scalar(
            select(PublishedConceptIdentity).where(
                PublishedConceptIdentity.family_key == "dartmouth"
            )
        )
        with pytest.raises(AppError) as publish_conflict:
            publish_concept_candidate(
                db,
                candidate_id=unresolved.id,
                reviewer_id="reviewer-2",
                review={"outcome": "approved"},
            )
        assert publish_conflict.value.code == "CONCEPT_PUBLICATION_UNRESOLVED"

        published_v2 = publish_concept_candidate(
            db,
            candidate_id=unresolved.id,
            reviewer_id="reviewer-2",
            review={"outcome": "approved", "basis": "确认定义边界变化"},
            supersedes_publication_id=prior.id,
        )
        assert published_v2.supersedes_id == prior.id
        prior_revision = db.get(ConceptRevision, prior.concept_revision_id)
        next_revision = db.get(
            ConceptRevision, published_v2.concept_revision_id
        )
        assert next_revision.concept_id == prior_revision.concept_id
        assert next_revision.revision == prior_revision.revision + 1
        resolved = resolve_candidate_revision(
            db,
            series_id="series_publication_b",
            section_id=sections_b[0].id,
            candidate=changed_concept.model_dump(),
        )
        assert resolved.id == published_v2.concept_revision_id
        assert resolved.supersedes_id == prior.concept_revision_id
