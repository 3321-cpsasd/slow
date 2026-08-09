from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.ai.contracts import (
    GeneratedPlan,
    PlanBook,
    PlanChapter,
    PlanMilestone,
    PlanMilestoneCriterion,
)
from app.core.errors import AppError
from app.infrastructure.tables import (
    Base,
    Competency,
    CourseVersion,
    CurriculumBaselineVersion,
    CurriculumSourceVersion,
    Discipline,
    ProgramVersion,
)
from app.modules.curriculum.baselines import (
    CurriculumBaselineReview,
    CurriculumBaselinePackage,
    CurriculumBaselineService,
)


PACKAGE_PATH = (
    Path(__file__).resolve().parents[1]
    / "curriculum_baselines"
    / "pku_cs_programming_practice_2025_v1.json"
)


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _reviewed_package() -> CurriculumBaselinePackage:
    package = CurriculumBaselineService.read_package(PACKAGE_PATH)
    payload = package.model_dump(by_alias=True, mode="json")
    payload["baselineKey"] = "test.reviewed.programming_practice"
    payload["title"] = "测试用已复核程序设计实习基准"
    payload["gaps"] = []
    for source in payload["sources"]:
        source["verificationStatus"] = "reviewed"
    for relation in payload["relations"]:
        relation["reviewStatus"] = "reviewed"
    return CurriculumBaselinePackage.model_validate(payload)


def _owner_review(package: CurriculumBaselinePackage) -> CurriculumBaselineReview:
    return CurriculumBaselineReview.model_validate(
        {
            "schemaVersion": "curriculum_baseline_review_v1",
            "baselineKey": package.baseline_key,
            "baselineVersion": package.version,
            "baselineContentHash": package.content_hash(),
            "reviewerId": "product_owner",
            "confirmationReference": "产品负责人在2026-08-09验收任务中确认来源关联与平台代码量规",
            "finalDecision": "approved",
            "sources": [
                {
                    "sourceKey": item.source_key,
                    "decision": "reviewed",
                    "note": "已人工核对权威机构、课程号、版本边界和提取定位。",
                }
                for item in package.sources
            ],
            "relations": [
                {
                    "fromConceptKey": item.from_concept_key,
                    "toConceptKey": item.to_concept_key,
                    "relationType": item.relation_type,
                    "decision": "reviewed",
                    "note": "测试夹具模拟人工逐条核对来源与关系方向。",
                }
                for item in package.relations
            ],
            "gaps": [
                {
                    "code": "COURSE_SYLLABUS_EFFECTIVE_VERSION_UNKNOWN",
                    "disposition": "accepted_risk",
                    "remainingBlockingStages": [],
                    "note": "只声明官方课程大纲检索快照及课程号精确关联，不声称它是2025版大纲。",
                },
                {
                    "code": "TEXTBOOK_CONTENT_SOURCE_NOT_CAPTURED",
                    "disposition": "deferred",
                    "remainingBlockingStages": ["knowledge_publication"],
                    "note": "指定教材只保留为课程元数据；知识主张另用公开可审计来源。",
                },
                {
                    "code": "CODE_ASSESSMENT_RUBRIC_MISSING",
                    "disposition": "resolved",
                    "remainingBlockingStages": [],
                    "note": "采用明确标记为非北大官方的Slow平台代码验证量规。",
                },
            ],
            "platformCodeAssessment": {
                "policyKey": "slow_code_task_v1",
                "authority": "slow_platform",
                "officialCoursePolicy": False,
                "dimensions": [
                    {"key": "functional_correctness", "label": "功能正确性", "weight": 50},
                    {"key": "boundary_cases", "label": "边界与异常情况", "weight": 20},
                    {"key": "algorithm_and_complexity", "label": "算法选择与复杂度", "weight": 15},
                    {"key": "code_quality", "label": "代码结构与可读性", "weight": 10},
                    {"key": "solution_explanation", "label": "解题说明", "weight": 5},
                ],
                "requiredDimensionMinimums": {
                    "functional_correctness": 60,
                    "boundary_cases": 50,
                },
                "passScore": 80,
            },
        }
    )


def _plan(bindings: list[list[str]]) -> GeneratedPlan:
    chapters = [
        PlanChapter(
            title=f"语义章 {index}",
            objective=f"完成能力结果 {index}",
            baseline_objective_ids=objective_keys,
        )
        for index, objective_keys in enumerate(bindings, 1)
    ]
    return GeneratedPlan(
        series_title="程序设计基础",
        rationale="按真实课程目标覆盖，不按知识节点数量拆分。",
        assumptions=[],
        confidence="high",
        books=[
            PlanBook(
                title="程序设计基础",
                topic="C程序设计",
                description="课程目标驱动的教材。",
                estimated_minutes=1200,
                chapters=chapters,
            )
        ],
        milestones=[
            PlanMilestone(
                title=f"里程碑 {index}",
                outcome=f"形成阶段能力 {index}",
                criteria=[
                    PlanMilestoneCriterion(
                        statement=chapters[min(index - 1, len(chapters) - 1)].objective,
                        book_position=1,
                        chapter_position=min(index, len(chapters)),
                    )
                ],
            )
            for index in range(1, 4)
        ],
    )


def test_real_official_source_package_imports_as_candidate_and_fails_closed():
    db = _db()
    service = CurriculumBaselineService(db)
    package = service.read_package(PACKAGE_PATH)

    baseline = service.import_candidate(package)

    assert baseline.status == "candidate"
    assert db.scalar(select(func.count()).select_from(Discipline)) == 1
    assert db.scalar(select(func.count()).select_from(ProgramVersion)) == 1
    assert db.scalar(select(func.count()).select_from(CourseVersion)) == 1
    assert db.scalar(select(func.count()).select_from(Competency)) == 4
    assert db.scalar(select(func.count()).select_from(CurriculumSourceVersion)) == 2
    assert "COURSE_SYLLABUS_EFFECTIVE_VERSION_UNKNOWN" in baseline.gaps_json
    assert service.select_for_plan(
        shelf=SimpleNamespace(domain="计算机", specialty="计算机科学与技术"),
        plan_input={"topic": "程序设计实习"},
    ) is None

    with pytest.raises(AppError) as error:
        service.publish(
            baseline.id,
            reviewer_id="reviewer_1",
            review_note="不能越过阻断缺口",
        )
    assert error.value.code == "CURRICULUM_BASELINE_BLOCKING_GAP"


def test_reviewed_baseline_drives_semantic_coverage_without_node_count_quota():
    db = _db()
    service = CurriculumBaselineService(db)
    baseline = service.import_candidate(_reviewed_package())
    baseline = service.publish(
        baseline.id,
        reviewer_id="human_reviewer",
        review_note="测试夹具明确模拟人工完成来源与关系复核。",
    )

    selected = service.select_for_plan(
        shelf=SimpleNamespace(domain="计算机", specialty="计算机科学与技术"),
        plan_input={"topic": "学习程序设计实习"},
    )
    assert selected.id == baseline.id
    context = service.planning_context(selected)
    required = context["coveragePolicy"]["requiredObjectiveKeys"]
    assert len(required) == 8

    # Three semantically coherent chapters can cover eight objectives. No rule
    # requires 20 nodes, five chapters, or four sections per chapter.
    generated = _plan([required[:2], required[2:5], required[5:]])
    service.validate_plan_coverage(selected, generated)


def test_missing_or_unknown_baseline_objectives_fail_before_plan_persistence():
    db = _db()
    service = CurriculumBaselineService(db)
    baseline = service.import_candidate(_reviewed_package())
    baseline = service.publish(
        baseline.id,
        reviewer_id="human_reviewer",
        review_note="测试夹具",
    )
    required = service.planning_context(baseline)["coveragePolicy"][
        "requiredObjectiveKeys"
    ]

    with pytest.raises(AppError) as missing:
        service.validate_plan_coverage(baseline, _plan([required[:2], required[2:4]]))
    assert missing.value.code == "CURRICULUM_BASELINE_COVERAGE_INCOMPLETE"

    with pytest.raises(AppError) as unknown:
        service.validate_plan_coverage(
            baseline,
            _plan([required[:2], [*required[2:], "invented_objective"]]),
        )
    assert unknown.value.code == "CURRICULUM_BASELINE_OBJECTIVE_UNKNOWN"


def test_same_baseline_version_cannot_be_silently_overwritten():
    db = _db()
    service = CurriculumBaselineService(db)
    package = service.read_package(PACKAGE_PATH)
    service.import_candidate(package)
    payload = package.model_dump(by_alias=True, mode="json")
    payload["title"] = "静默改写"

    with pytest.raises(AppError) as error:
        service.import_candidate(CurriculumBaselinePackage.model_validate(payload))
    assert error.value.code == "CURRICULUM_BASELINE_VERSION_CONFLICT"


def test_owner_review_preserves_gaps_by_stage_and_publishes_frozen_candidate():
    db = _db()
    service = CurriculumBaselineService(db)
    package = service.read_package(PACKAGE_PATH)
    baseline = service.import_candidate(package)
    review_payload = _owner_review(package).model_dump(by_alias=True, mode="json")
    review_payload["relations"][0]["decision"] = "rejected"
    review_payload["relations"][0]["note"] = (
        "人工完成复核但拒绝仅由课程顺序推断出的前置方向。"
    )
    service.apply_review(
        baseline.id,
        CurriculumBaselineReview.model_validate(review_payload),
    )
    published = service.publish(
        baseline.id,
        reviewer_id="product_owner",
        review_note="确认按审核清单发布课程范围；知识事实仍走独立门禁。",
    )

    assert published.status == "published"
    context = service.planning_context(published)
    rubric = context["course"]["platformCodeAssessment"]
    assert rubric["authority"] == "slow_platform"
    assert rubric["officialCoursePolicy"] is False
    assert sum(item["weight"] for item in rubric["dimensions"]) == 100
    assert len(context["relations"]) == len(package.relations) - 1


def test_review_manifest_must_match_frozen_version_and_cover_every_relation():
    db = _db()
    service = CurriculumBaselineService(db)
    package = service.read_package(PACKAGE_PATH)
    baseline = service.import_candidate(package)
    payload = _owner_review(package).model_dump(by_alias=True, mode="json")
    payload["baselineContentHash"] = "0" * 64

    with pytest.raises(AppError) as mismatch:
        service.apply_review(
            baseline.id,
            CurriculumBaselineReview.model_validate(payload),
        )
    assert mismatch.value.code == "CURRICULUM_BASELINE_REVIEW_VERSION_MISMATCH"

    payload = _owner_review(package).model_dump(by_alias=True, mode="json")
    payload["relations"] = payload["relations"][:-1]
    with pytest.raises(AppError) as incomplete:
        service.apply_review(
            baseline.id,
            CurriculumBaselineReview.model_validate(payload),
        )
    assert incomplete.value.code == "CURRICULUM_BASELINE_RELATION_REVIEW_INCOMPLETE"
