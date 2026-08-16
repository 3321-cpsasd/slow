import asyncio

import pytest
from pydantic import ValidationError

from app.ai.contracts import (
    GeneratedChapter,
    GeneratedConceptCandidate,
    GeneratedSectionOutline,
)
from app.ai.local_adapter import LocalDemoAdapter
from app.modules.curriculum.policy import CHAPTER_SECTION_POLICY


def _sections(count: int) -> list[GeneratedSectionOutline]:
    return [
        GeneratedSectionOutline(
            title=f"知识点 {position}",
            question=f"核心问题 {position} 是什么？",
            objectives=[f"验证目标 {position}"],
        )
        for position in range(1, count + 1)
    ]


@pytest.mark.parametrize("count", [2, 3, 5, 6, 12])
def test_chapter_section_count_accepts_soft_range(count):
    chapter = GeneratedChapter(sections=_sections(count))
    assert len(chapter.sections) == count


@pytest.mark.parametrize("count", [1, 13])
def test_chapter_section_count_rejects_only_technical_anomalies(count):
    with pytest.raises(ValidationError):
        GeneratedChapter(sections=_sections(count))


@pytest.mark.parametrize(
    ("count", "level"),
    [(2, "light"), (3, "typical"), (5, "typical"), (6, "extended")],
)
def test_workload_hint_does_not_turn_typical_range_into_gate(count, level):
    hint = CHAPTER_SECTION_POLICY.workload(count)
    assert hint["level"] == level
    assert hint["typicalRange"] == [3, 5]
    assert hint["technicalRange"] == [2, 12]


def test_historical_partial_chapter_is_visible_but_explicitly_anomalous():
    hint = CHAPTER_SECTION_POLICY.workload(1)
    assert hint["level"] == "anomalous"
    assert "显式检查" in hint["message"]


def test_candidate_objectives_require_aligned_capability_dimensions():
    candidate = GeneratedConceptCandidate(
        candidate_key="recursion",
        label="递归",
        definition="递归通过缩小问题并抵达基本情形完成求解",
        scope="解释终止机制与迁移边界",
    )

    with pytest.raises(ValidationError):
        GeneratedSectionOutline(
            title="递归终止",
            question="递归为什么能够终止？",
            objectives=["解释基本情形", "解释规模缩小"],
            concept_candidate=candidate,
            objective_dimensions=["mechanism"],
        )

    outline = GeneratedSectionOutline(
        title="递归终止",
        question="递归为什么能够终止？",
        objectives=["解释基本情形", "迁移到深度优先搜索"],
        concept_candidate=candidate,
        objective_dimensions=["mechanism", "transfer"],
    )
    assert outline.objective_dimensions == ["mechanism", "transfer"]


def test_local_demo_dimension_uses_capability_verb_not_topic_keywords():
    adapter = LocalDemoAdapter()
    context = {
        "generationContext": {
            "curriculum": {
                "book": {
                    "topic": "递归：从终止机制到搜索与动态规划迁移"
                }
            }
        },
        "title": "核心对象",
        "objective": "解释 递归：从终止机制到搜索与动态规划迁移 的核心对象及关系",
    }

    chapter = asyncio.run(adapter.chapter(context, []))

    assert {
        dimension
        for section in chapter.sections
        for dimension in section.objective_dimensions
    } == {"mechanism"}
