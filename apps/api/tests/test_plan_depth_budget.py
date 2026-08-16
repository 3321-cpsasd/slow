import asyncio

import pytest

from app.ai.context import policy_for
from app.ai.contracts import (
    GeneratedOverviewPlan,
    GeneratedPlan,
    PlanBook,
    PlanChapter,
    PlanMilestone,
    PlanMilestoneCriterion,
)
from app.ai.local_adapter import LocalDemoAdapter
from app.ai.openai_adapter import OpenAiAdapter
from app.core.errors import AppError
from app.modules.curriculum.series_planning import require_plan_within_route_budget


def _book(position: int) -> PlanBook:
    return PlanBook(
        title=f"第 {position} 本",
        topic="测试主题",
        description="用于验证学习深度路线预算。",
        estimated_minutes=240,
        chapters=[
            PlanChapter(title="核心对象", objective="解释核心对象"),
            PlanChapter(title="关键边界", objective="识别关键边界"),
        ],
    )


def _milestone(position: int = 1) -> PlanMilestone:
    return PlanMilestone(
        title="阶段能力",
        outcome="形成可验证的阶段能力",
        criteria=[
            PlanMilestoneCriterion(
                statement="完成阶段验证",
                book_position=position,
                chapter_position=1,
            )
        ],
    )


def test_overview_schema_accepts_only_one_book():
    with pytest.raises(ValueError):
        GeneratedOverviewPlan(
            series_title="过长路线",
            rationale="不应通过",
            assumptions=[],
            confidence="high",
            books=[_book(1), _book(2)],
            milestones=[_milestone(1), _milestone(2)],
        )


def test_openai_adapter_uses_compact_schema_for_overview():
    async def run():
        adapter = OpenAiAdapter("", "test-model")
        captured = {}

        async def fake_parse(schema, prompt, payload, tokens):
            captured["schema"] = schema
            captured["prompt"] = prompt
            return object()

        adapter._parse = fake_parse
        await adapter.plan({"topic": "AI 算力", "depth": "overview"}, [])
        return captured

    captured = asyncio.run(run())

    assert captured["schema"] is GeneratedOverviewPlan
    assert "只生成 1 本精简教材" in captured["prompt"]


def test_route_budget_fails_closed_when_overview_contains_multiple_books():
    generated = GeneratedPlan(
        series_title="过长路线",
        rationale="候选路线忽略了用户选择",
        assumptions=[],
        confidence="high",
        books=[_book(1), _book(2)],
        milestones=[_milestone(1), _milestone(2), _milestone(2)],
    )

    with pytest.raises(AppError) as captured:
        require_plan_within_route_budget(
            generated,
            policy_for("plan", "overview").depth_policy,
        )

    assert captured.value.code == "PLAN_DEPTH_BUDGET_EXCEEDED"
    assert captured.value.retryable is True


def test_local_demo_overview_is_one_compact_book():
    generated = asyncio.run(
        LocalDemoAdapter().plan(
            {"topic": "AI 算力", "depth": "overview"},
            [],
        )
    )

    assert len(generated.books) == 1
    assert len(generated.books[0].chapters) == 2
    assert len(generated.milestones) == 2
