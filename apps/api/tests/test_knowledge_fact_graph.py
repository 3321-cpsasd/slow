import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.ai.contracts import GeneratedSectionOutline
from app.infrastructure.tables import (
    AssessmentTarget,
    Base,
    Book,
    Chapter,
    ConceptObjectiveBinding,
    ConceptRelationVersion,
    ConceptRevision,
    KnowledgeClaimBinding,
    KnowledgeGap,
    KnowledgeGraphRelease,
    KnowledgeSourceVersion,
    LearningContractConcept,
    LearningContractObjective,
    LearningMissionVersion,
    LearningObjective,
    LearningPlan,
    Section,
    Series,
    Shelf,
    SourceClaimVersion,
    User,
)
from app.modules.curriculum.baselines import (
    CurriculumBaselinePackage,
    CurriculumBaselineService,
)
from app.modules.curriculum.chapter_planning import section_objectives_payload
from app.modules.knowledge.fact_graph import (
    KnowledgeFactGraphService,
    KnowledgeGraphReviewManifest,
    KnowledgeGraphSlicePackage,
)
from app.modules.learning.contracts import ensure_learning_contract


BASELINE_PATH = (
    Path(__file__).resolve().parents[1]
    / "curriculum_baselines"
    / "pku_cs_programming_practice_2025_v1.json"
)
REAL_SLICE_PATH = (
    Path(__file__).resolve().parents[1]
    / "knowledge_graph_slices"
    / "pku_recursion_search_dp_v1.json"
)


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _baseline(db: Session, *, published: bool = True):
    original = CurriculumBaselineService.read_package(BASELINE_PATH)
    payload = original.model_dump(by_alias=True, mode="json")
    payload["baselineKey"] = "test.pku.programming.knowledge"
    payload["title"] = "测试用北大程序设计知识基准"
    payload["gaps"] = []
    for source in payload["sources"]:
        source["verificationStatus"] = "reviewed"
    for relation in payload["relations"]:
        relation["reviewStatus"] = "reviewed"
    service = CurriculumBaselineService(db)
    baseline = service.import_candidate(CurriculumBaselinePackage.model_validate(payload))
    if published:
        baseline = service.publish(
            baseline.id,
            reviewer_id="curriculum_reviewer",
            review_note="测试夹具模拟用户已确认大纲适用性与平台量规。",
        )
    return baseline


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _package(
    baseline_id: str,
    *,
    verified_bindings: bool = True,
    reviewed_relations: bool = True,
) -> KnowledgeGraphSlicePackage:
    verification = "verified" if verified_bindings else "candidate"
    relation_review = "reviewed" if reviewed_relations else "candidate"
    payload = {
        "schemaVersion": "knowledge_graph_slice_v1",
        "baselineVersionId": baseline_id,
        "version": 1,
        "title": "递归、图搜索与动态规划最小知识事实切片",
        "status": "candidate",
        "sources": [
            {
                "sourceKey": "open_recursion",
                "sourceKind": "open_textbook",
                "title": "Open recursion chapter",
                "authority": "Open textbook project",
                "url": "https://example.org/open/recursion",
                "versionLabel": "snapshot-2026-08-09",
                "retrievalDate": "2026-08-09",
                "contentDigest": _digest("recursion source snapshot"),
                "rightsStatus": "open_access",
                "verificationStatus": "reviewed",
                "provenance": {"fixture": True},
            },
            {
                "sourceKey": "open_algorithms",
                "sourceKind": "open_textbook",
                "title": "Open graph and dynamic programming chapters",
                "authority": "Open textbook project",
                "url": "https://example.org/open/algorithms",
                "versionLabel": "snapshot-2026-08-09",
                "retrievalDate": "2026-08-09",
                "contentDigest": _digest("algorithms source snapshot"),
                "rightsStatus": "open_access",
                "verificationStatus": "reviewed",
                "provenance": {"fixture": True},
            },
        ],
        "concepts": [
            {
                "key": "recursion",
                "revision": 1,
                "label": "递归",
                "definition": "递归过程通过更小规模的同类调用推进，并由基本情形终止。",
                "scope": {
                    "pilot": True,
                    "rankPolicy": {
                        "version": "knowledge_rank_policy_v1",
                        "capabilityScope": "解释递归机制并判断其终止条件",
                        "rankCeiling": "platinum",
                        "dimensionRanks": {
                            "recognition": "bronze",
                            "mechanism": "silver",
                            "application": "gold",
                            "boundary": "platinum",
                        },
                    },
                },
                "boundaries": ["终止性需要同时检查基本情形与规模推进。"],
                "objectiveKeys": [
                    "solve_with_enumeration_recursion_and_search",
                    "model_and_solve_with_dynamic_programming",
                ],
                "claimKeys": ["recursion_base_and_progress"],
            },
            {
                "key": "graph_search",
                "revision": 1,
                "label": "深度优先与广度优先搜索",
                "definition": "图搜索从起点系统访问可达顶点，并通过访问标记避免重复处理。",
                "scope": {
                    "pilot": True,
                    "rankPolicy": {
                        "version": "knowledge_rank_policy_v1",
                        "capabilityScope": "选择并执行标准图搜索策略",
                        "rankCeiling": "platinum",
                        "dimensionRanks": {
                            "recognition": "bronze",
                            "mechanism": "silver",
                            "application": "gold",
                            "boundary": "platinum",
                        },
                    },
                },
                "boundaries": ["复杂度结论依赖图表示与访问模型。"],
                "objectiveKeys": ["solve_with_enumeration_recursion_and_search"],
                "claimKeys": ["dfs_visit_and_mark"],
            },
            {
                "key": "dynamic_programming",
                "revision": 1,
                "label": "动态规划",
                "definition": "动态规划保存重复子问题的结果，并按状态依赖组织求值。",
                "scope": {
                    "pilot": True,
                    "rankPolicy": {
                        "version": "knowledge_rank_policy_v1",
                        "capabilityScope": "建立并求解标准动态规划状态模型",
                        "rankCeiling": "diamond",
                        "dimensionRanks": {
                            "recognition": "bronze",
                            "mechanism": "silver",
                            "application": "gold",
                            "boundary": "platinum",
                            "transfer": "diamond",
                        },
                    },
                },
                "boundaries": ["状态必须包含决定后续结果所需的全部信息。"],
                "objectiveKeys": ["model_and_solve_with_dynamic_programming"],
                "claimKeys": ["dp_reuses_subproblems"],
            },
        ],
        "relations": [
            {
                "key": "recursion_applies_to_dfs",
                "fromConceptKey": "recursion",
                "toConceptKey": "graph_search",
                "relationType": "applies_to",
                "reviewStatus": relation_review,
                "claimKeys": ["dfs_visit_and_mark"],
                "provenance": {"reviewBasis": "open source locator"},
            },
            {
                "key": "recursion_contrasts_dp",
                "fromConceptKey": "recursion",
                "toConceptKey": "dynamic_programming",
                "relationType": "contrasts_with",
                "reviewStatus": relation_review,
                "claimKeys": ["dp_reuses_subproblems"],
                "provenance": {"reviewBasis": "open source locator"},
            },
        ],
        "claims": [
            {
                "key": "recursion_base_and_progress",
                "statement": "递归算法需要基本情形，并让递归调用向基本情形推进。",
                "claimKind": "mechanism",
                "conceptKeys": ["recursion"],
                "relationKeys": [],
                "strict": True,
                "sourceBindings": [
                    {
                        "sourceKey": "open_recursion",
                        "locatorType": "section",
                        "locator": {"section": "recursion/base-case"},
                        "supportType": "supports",
                        "verificationStatus": verification,
                        "review": {"reviewer": "source_reviewer"},
                    }
                ],
            },
            {
                "key": "dfs_visit_and_mark",
                "statement": "深度优先搜索访问尚未访问的邻接顶点，访问标记可避免在环中重复遍历。",
                "claimKind": "mechanism",
                "conceptKeys": ["graph_search"],
                "relationKeys": ["recursion_applies_to_dfs"],
                "strict": True,
                "sourceBindings": [
                    {
                        "sourceKey": "open_algorithms",
                        "locatorType": "section",
                        "locator": {"section": "graph/dfs"},
                        "supportType": "supports",
                        "verificationStatus": verification,
                        "review": {"reviewer": "source_reviewer"},
                    }
                ],
            },
            {
                "key": "dp_reuses_subproblems",
                "statement": "动态规划通过记忆化或制表保存并复用重复子问题的结果。",
                "claimKind": "mechanism",
                "conceptKeys": ["dynamic_programming"],
                "relationKeys": ["recursion_contrasts_dp"],
                "strict": True,
                "sourceBindings": [
                    {
                        "sourceKey": "open_algorithms",
                        "locatorType": "section",
                        "locator": {"section": "dynamic-programming/overview"},
                        "supportType": "supports",
                        "verificationStatus": verification,
                        "review": {"reviewer": "source_reviewer"},
                    }
                ],
            },
        ],
        "declaredGaps": [
            {
                "code": "DP_STATE_KEY_SUFFICIENCY_UNPROVEN",
                "severity": "warning",
                "subjectKind": "concept",
                "subjectKey": "dynamic_programming",
                "message": "具体题目的状态键充分性仍须按题目单独验证，不能从通用定义推出。",
            }
        ],
        "reviewContext": {"purpose": "M2 deterministic thin slice fixture"},
    }
    return KnowledgeGraphSlicePackage.model_validate(payload)


def _review(
    release,
    package: KnowledgeGraphSlicePackage,
    *,
    decision: str = "approved",
) -> KnowledgeGraphReviewManifest:
    gaps = json.loads(release.gaps_json)
    return KnowledgeGraphReviewManifest.model_validate(
        {
            "schemaVersion": "knowledge_graph_review_v1",
            "releaseId": release.id,
            "contentHash": release.content_hash,
            "decision": decision,
            "reviewerId": "human_knowledge_reviewer",
            "reviewedAt": "2026-08-09T20:00:00+08:00",
            "reviewNote": "逐条核对来源、主张、关系与保留缺口。",
            "acceptedSourceKeys": [item.source_key for item in package.sources],
            "acceptedClaimKeys": [item.key for item in package.claims],
            "acceptedRelationKeys": [item.key for item in package.relations],
            "gapDispositions": [
                {
                    "gapId": item["id"],
                    "disposition": "acknowledged_warning",
                    "rationale": "保留为具体题目生成时必须检查的显式边界。",
                }
                for item in gaps
                if item["severity"] == "warning"
            ],
        }
    )


def _section_with_explicit_knowledge(
    db: Session,
    baseline,
    *,
    concept_key: str = "recursion",
    objective_key: str = "solve_with_enumeration_recursion_and_search",
):
    db.add(User(id="knowledge_user", name="Knowledge Learner"))
    db.flush()
    db.add(
        Shelf(
            id="knowledge_shelf",
            user_id="knowledge_user",
            name="计算机",
            domain="计算机",
            specialty="计算机科学与技术",
            tags_json="[]",
        )
    )
    db.flush()
    db.add(
        LearningPlan(
            id="knowledge_plan",
            shelf_id="knowledge_shelf",
            topic="程序设计实习",
            role="学生",
            experience="",
            purpose="M2 薄切片",
            depth="deep",
            details="",
            assumptions_json="[]",
            confidence="high",
            status="active",
        )
    )
    db.flush()
    mission = LearningMissionVersion(
        id="knowledge_mission",
        plan_id="knowledge_plan",
        user_id="knowledge_user",
        version=1,
        status="active",
        why="M2 薄切片",
        target_capabilities_json="[]",
        constraints_json="{}",
        out_of_scope_json="[]",
        assumptions_json="[]",
        learner_context_json="{}",
        inferred_fields_json="[]",
        provenance_json="{}",
        schema_version="mission_v1",
        payload_hash="b" * 64,
    )
    db.add(mission)
    db.flush()
    db.add(
        Series(
            id="knowledge_series",
            plan_id="knowledge_plan",
            shelf_id="knowledge_shelf",
            title="程序设计",
            rationale="M2",
            initial_mission_version_id=mission.id,
        )
    )
    db.flush()
    db.add(
        Book(
            id="knowledge_book",
            series_id="knowledge_series",
            shelf_id="knowledge_shelf",
            position=1,
            title="算法方法",
            topic="递归与动态规划",
            description="",
            estimated_minutes=60,
        )
    )
    db.flush()
    chapter = Chapter(
        id="knowledge_chapter",
        book_id="knowledge_book",
        position=1,
        title="递归",
        objective="解释递归机制",
    )
    db.add(chapter)
    db.flush()
    CurriculumBaselineService(db).bind_chapter_objectives(
        chapter_id=chapter.id,
        baseline=baseline,
        objective_keys=[objective_key],
    )
    outlines = [
        GeneratedSectionOutline(
            title="递归的基本情形",
            question="递归为什么能够终止？",
            objectives=["解释递归的基本情形和推进条件"],
            baseline_concept_key=concept_key,
            baseline_objective_key=objective_key,
        ),
        GeneratedSectionOutline(
            title="DFS 的访问标记",
            question="DFS 为什么不会在环中无限重复？",
            objectives=["解释 DFS 的递归访问和访问标记"],
            baseline_concept_key="graph_search",
            baseline_objective_key=objective_key,
        ),
    ]
    sections = []
    for position, outline in enumerate(outlines, 1):
        section = Section(
            id=f"knowledge_section_{position}",
            chapter_id=chapter.id,
            position=position,
            title=outline.title,
            question=outline.question,
            objectives_json=json.dumps(
                section_objectives_payload(outline), ensure_ascii=False
            ),
        )
        db.add(section)
        sections.append(section)
    db.flush()
    return sections, mission, outlines


def test_verified_slice_publishes_stable_versions_and_typed_relations():
    db = _db()
    baseline = _baseline(db)
    service = KnowledgeFactGraphService(db)

    package = _package(baseline.id)
    release = service.import_candidate(package)
    assert release.status == "candidate"
    assert db.scalar(select(func.count()).select_from(ConceptRevision)) == 3
    assert db.scalar(select(func.count()).select_from(LearningObjective)) == 2
    assert db.scalar(select(func.count()).select_from(ConceptRelationVersion)) == 2
    assert db.scalar(select(func.count()).select_from(KnowledgeSourceVersion)) == 2
    assert db.scalar(select(func.count()).select_from(SourceClaimVersion)) == 3
    assert db.scalar(select(func.count()).select_from(KnowledgeClaimBinding)) == 3
    assert db.scalar(select(func.count()).select_from(ConceptObjectiveBinding)) == 4
    warning = db.scalar(select(KnowledgeGap))
    assert warning.severity == "warning"

    published = service.publish(
        release.id,
        review=_review(release, package),
    )
    assert published.status == "published"
    assert all(
        item.verification_status == "reviewed"
        for item in db.scalars(select(ConceptRevision)).all()
    )
    assert all(
        item.verification_status == "reviewed"
        for item in db.scalars(select(LearningObjective)).all()
    )
    assert all(
        item.status == "published"
        for item in db.scalars(select(ConceptRelationVersion)).all()
    )
    assert all(
        item.status == "published" and item.trust_state == "verified"
        for item in db.scalars(select(SourceClaimVersion)).all()
    )


def test_real_opendsa_slice_has_stable_claim_scope_and_only_declared_warnings():
    db = _db()
    baseline = _baseline(db)
    real = KnowledgeFactGraphService.read_package(REAL_SLICE_PATH)
    payload = real.model_dump(by_alias=True, mode="json")
    payload["baselineVersionId"] = baseline.id
    package = KnowledgeGraphSlicePackage.model_validate(payload)

    service = KnowledgeFactGraphService(db)
    release = service.import_candidate(package)
    gaps = db.scalars(select(KnowledgeGap)).all()
    assert len(gaps) == 7
    assert {item.severity for item in gaps} == {"warning"}
    for claim in db.scalars(select(SourceClaimVersion)).all():
        scope = json.loads(claim.scope_json)
        assert scope["conceptRevisionIds"]
        assert scope["learningObjectiveIds"]
        assert "relationVersionIds" in scope

    published = service.publish(
        release.id,
        review=_review(release, package),
    )
    assert published.status == "published"


def test_explicit_section_keys_materialize_verified_contract_without_provisional_seed():
    db = _db()
    baseline = _baseline(db)
    service = KnowledgeFactGraphService(db)
    package = _package(baseline.id)
    release = service.import_candidate(package)
    service.publish(
        release.id,
        review=_review(release, package),
    )
    sections, mission, outlines = _section_with_explicit_knowledge(db, baseline)
    allowlist = service.validate_chapter_outline_identities(
        sections[0].chapter_id, outlines
    )
    assert {(item["conceptKey"], item["objectiveKey"]) for item in allowlist} == {
        ("recursion", "solve_with_enumeration_recursion_and_search"),
        ("graph_search", "solve_with_enumeration_recursion_and_search"),
    }

    contracts = [
        ensure_learning_contract(
            db,
            section,
            mission_version_id=mission.id,
        )
        for section in sections
    ]
    targets = db.scalars(select(AssessmentTarget)).all()
    assert all(item.provenance_mode == "published_knowledge_graph" for item in contracts)
    assert all(item.lineage_status == "verified" for item in contracts)
    assert all(item.identity_status == "published_knowledge_graph" for item in targets)
    assert all(
        db.get(ConceptRevision, item.concept_revision_id).verification_status
        == "reviewed"
        for item in targets
    )
    assert db.scalar(select(func.count()).select_from(LearningContractConcept)) == 2
    assert db.scalar(select(func.count()).select_from(LearningContractObjective)) == 2
    assert db.scalar(
        select(func.count()).select_from(ConceptRevision).where(
            ConceptRevision.provenance_mode == "m1_provisional"
        )
    ) == 0


def test_chapter_scope_freezes_only_explicit_planned_concepts_and_rejects_bad_pair():
    db = _db()
    baseline = _baseline(db)
    service = KnowledgeFactGraphService(db)
    package = _package(baseline.id)
    release = service.import_candidate(package)
    service.publish(release.id, review=_review(release, package))
    sections, _mission, _outlines = _section_with_explicit_knowledge(db, baseline)
    chapter_id = sections[0].chapter_id

    scope = service.bind_chapter_identity_scope(
        chapter_id=chapter_id,
        baseline_version_id=baseline.id,
        objective_keys=["solve_with_enumeration_recursion_and_search"],
        concept_keys=["recursion"],
    )
    assert {(item["conceptKey"], item["objectiveKey"]) for item in scope["pairs"]} == {
        ("recursion", "solve_with_enumeration_recursion_and_search")
    }
    assert {
        (item["conceptKey"], item["objectiveKey"])
        for item in service.chapter_identity_allowlist(chapter_id)
    } == {("recursion", "solve_with_enumeration_recursion_and_search")}

    with pytest.raises(AppError) as error:
        service.bind_chapter_identity_scope(
            chapter_id=chapter_id,
            baseline_version_id=baseline.id,
            objective_keys=["solve_with_enumeration_recursion_and_search"],
            concept_keys=["dynamic_programming"],
        )
    assert error.value.code == "CHAPTER_KNOWLEDGE_IDENTITY_OUT_OF_SCOPE"


def test_missing_verified_source_creates_gap_and_fails_closed_without_state_changes():
    db = _db()
    baseline = _baseline(db)
    service = KnowledgeFactGraphService(db)
    package = _package(baseline.id, verified_bindings=False)
    release = service.import_candidate(package)

    blocking = db.scalars(
        select(KnowledgeGap).where(KnowledgeGap.severity == "blocking")
    ).all()
    assert len(blocking) == 3
    with pytest.raises(AppError) as error:
        service.publish(
            release.id,
            review=_review(release, package),
        )
    assert error.value.code == "KNOWLEDGE_GRAPH_BLOCKING_GAP"
    db.refresh(release)
    assert release.status == "candidate"
    assert all(
        item.verification_status == "candidate"
        for item in db.scalars(select(ConceptRevision)).all()
    )
    assert all(
        item.status == "candidate"
        for item in db.scalars(select(SourceClaimVersion)).all()
    )


def test_incomplete_independent_review_rejects_atomically():
    db = _db()
    baseline = _baseline(db)
    service = KnowledgeFactGraphService(db)
    package = _package(baseline.id, reviewed_relations=False)
    release = service.import_candidate(package)
    review_payload = _review(release, package).model_dump(by_alias=True, mode="json")
    review_payload["acceptedRelationKeys"] = review_payload[
        "acceptedRelationKeys"
    ][:-1]

    with pytest.raises(AppError) as error:
        service.publish(
            release.id,
            review=KnowledgeGraphReviewManifest.model_validate(review_payload),
        )
    assert error.value.code == "KNOWLEDGE_GRAPH_REVIEW_RELATION_COVERAGE_MISMATCH"
    db.refresh(release)
    assert release.status == "candidate"
    assert all(
        item.status == "candidate"
        for item in db.scalars(select(ConceptRelationVersion)).all()
    )


def test_slice_cannot_escape_baseline_concept_scope():
    db = _db()
    baseline = _baseline(db)
    package = _package(baseline.id)
    payload = package.model_dump(by_alias=True, mode="json")
    payload["concepts"][0]["key"] = "invented_concept"
    payload["claims"][0]["conceptKeys"] = ["invented_concept"]
    payload["relations"][0]["fromConceptKey"] = "invented_concept"
    payload["relations"][1]["fromConceptKey"] = "invented_concept"
    candidate = KnowledgeGraphSlicePackage.model_validate(payload)

    with pytest.raises(AppError) as error:
        KnowledgeFactGraphService(db).import_candidate(candidate)
    assert error.value.code == "KNOWLEDGE_GRAPH_BASELINE_SCOPE_VIOLATION"
    assert db.scalar(select(func.count()).select_from(KnowledgeGraphRelease)) == 0
