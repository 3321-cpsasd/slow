import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from app.ai.anthropic_adapter import AnthropicAdapter
from app.ai.contracts import (
    GeneratedContent,
    GeneratedQuiz,
    GeneratedRemediationContent,
    GeneratedSourceRepair,
    TeachingBlueprint,
)
from app.ai.metering import AiUsageRecorder
from app.ai.context import policy_for
from app.ai.local_adapter import LocalDemoAdapter
from app.ai.openai_adapter import OpenAiAdapter
from app.ai.port import ProviderCapabilities
from app.auth.context import Principal
from app.core.errors import AiError
from app.infrastructure.database import build_database
from app.infrastructure.tables import (
    AiInvocation,
    AiUsageMeasurement,
    Base,
    User,
)


class StructuredAnswer(BaseModel):
    value: str


class FakeProviderError(RuntimeError):
    def __init__(self, *, status_code=None, code=""):
        super().__init__("provider failure")
        self.status_code = status_code
        self.code = code


def test_curriculum_hierarchy_is_versioned_and_embedded_in_generation_prompts():
    plan_policy = policy_for("plan", "deep")
    chapter_policy = policy_for("chapter", "deep")
    replan_policy = policy_for("book_replan", "deep")
    lesson_policy = policy_for("lesson_content", "deep")
    assert plan_policy.version == "plan_context_v2"
    assert chapter_policy.version == "chapter_context_v2"
    assert replan_policy.version == "book_replan_context_v2"
    assert lesson_policy.version == "lesson_content_context_v2"
    assert any("coherent theme" in item for item in plan_policy.allowed_uses)
    assert any("focal knowledge point" in item for item in chapter_policy.allowed_uses)
    assert any("weak prerequisites" in item for item in lesson_policy.allowed_uses)

    async def run():
        adapter = OpenAiAdapter("", "test-model")
        calls = []

        async def fake_parse(schema, prompt, payload, tokens):
            calls.append((schema.__name__, prompt, payload, tokens))
            return SimpleNamespace()

        adapter._parse = fake_parse
        await adapter.plan({"topic": "信息可视化"}, [])
        await adapter.chapter({"title": "视觉编码", "objective": "理解视觉通道"}, [])
        await adapter.replan_book({"title": "信息可视化基础"}, [])
        await adapter.teaching_blueprint({"question": "位置编码为什么准确？"}, [])
        await adapter.lesson_content({"question": "位置编码为什么准确？"}, [])
        return calls

    calls = asyncio.run(run())
    prompts = {name: prompt for name, prompt, _payload, _tokens in calls}
    assert "一个已确认学习目标形成一个系列" in prompts["GeneratedPlan"]
    assert "每本书围绕一个完整学习主题" in prompts["GeneratedPlan"]
    assert "每章是一组相关知识点的聚合" in prompts["GeneratedPlan"]
    assert "每节必须有一个核心知识点和一个主要验证问题" in prompts["GeneratedChapter"]
    assert "这不意味着把知识点当成孤立节点" in prompts["GeneratedChapter"]
    assert "不得仅因它们是讲授阶段就生成新的并列小节" in prompts["GeneratedChapter"]
    assert "每个未来章必须是一组相关知识点的聚合" in prompts["ReplannedBook"]
    assert "section.question 是本节的核心知识锚点，不是知识孤岛" in prompts["TeachingBlueprint"]
    assert "对薄弱或缺失的关联补充足够脚手架" in prompts["GeneratedContent"]
    assert "支撑性关联知识不得静默变成新的 assessmentTargets" in prompts["GeneratedContent"]


def test_teaching_blueprint_requires_every_learning_role():
    with pytest.raises(ValidationError):
        TeachingBlueprint.model_validate(
            {
                "narrative_thread": "围绕同一个问题逐步建立判断模型。",
                "opening_move": "先给出一个问题。",
                "core_model": "对象经过机制产生结果，并受到边界条件约束。",
                "recap_prompt": "请复述这个模型。",
                "blocks": [
                    {"kind": "text", "role": "conclusion", "purpose": "回答问题", "heading_intent": "先作判断"},
                    {"kind": "text", "role": "mechanism", "purpose": "解释机制", "heading_intent": "为何如此"},
                    {"kind": "text", "role": "example", "purpose": "推进例子", "heading_intent": "放进场景"},
                    {"kind": "text", "role": "boundary", "purpose": "说明边界", "heading_intent": "何时失效"},
                    {"kind": "text", "role": "transition", "purpose": "连接段落", "heading_intent": "继续追问"},
                ],
            }
        )


def test_local_demo_teaching_blueprint_returns_a_complete_blueprint():
    blueprint = asyncio.run(
        LocalDemoAdapter().teaching_blueprint(
            {
                "generationContext": {
                    "learner": {
                        "preferences": {"formatPreferences": ["diagram"]}
                    }
                }
            },
            [],
        )
    )

    assert isinstance(blueprint, TeachingBlueprint)
    assert {block.role for block in blueprint.blocks} >= {
        "conclusion",
        "mechanism",
        "example",
        "boundary",
        "practice",
    }


class FakeChatCompletions:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []

    async def create(self, **options):
        self.calls.append(options)
        content = next(self.outputs)
        if isinstance(content, BaseException):
            raise content
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
        )


async def chat_adapter(outputs, *, reasoning_mode="optional"):
    adapter = OpenAiAdapter(
        "test-key",
        "qwen-test",
        "https://workspace.example/compatible-mode/v1",
        capabilities=ProviderCapabilities(
            protocol="openai",
            api_mode="chat_completions",
            structured_output=True,
            streaming=True,
            reasoning_mode=reasoning_mode,
        ),
    )
    await adapter.close()
    completions = FakeChatCompletions(outputs)
    adapter.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )
    return adapter, completions


def test_openai_adapter_maps_invalid_key_to_actionable_error():
    error = OpenAiAdapter._provider_error(
        FakeProviderError(status_code=400, code="InvalidApiKey")
    )

    assert error is not None
    assert error.code == "AI_PROVIDER_AUTH_FAILED"
    assert error.retryable is False
    assert "AI 设置" in str(error)


def test_openai_adapter_does_not_misclassify_local_validation_errors():
    assert OpenAiAdapter._provider_error(ValueError("invalid JSON")) is None


def test_openai_chat_repairs_invalid_schema_with_validation_feedback():
    async def run():
        adapter, completions = await chat_adapter(
            ['{"value":7}', '{"value":"ok"}']
        )
        answer = await adapter._parse(
            StructuredAnswer,
            "Return JSON.",
            {"question": "test"},
            100,
        )
        return answer, completions.calls, adapter.structured_trace()

    answer, calls, trace = asyncio.run(run())

    assert answer.value == "ok"
    assert len(calls) == 2
    assert "JSON 结构修复器" in calls[1]["messages"][0]["content"]
    repair_payload = json.loads(calls[1]["messages"][1]["content"])
    assert repair_payload["invalid_output"] == '{"value":7}'
    assert repair_payload["validation_errors"][0]["path"] == "value"
    assert len(trace) == 1
    assert trace[0]["schema"] == "StructuredAnswer"
    assert trace[0]["attempts"] == 2
    assert trace[0]["repairAttempts"] == 1
    assert trace[0]["outcome"] == "succeeded"
    assert len(trace[0]["invalidOutputDigests"]) == 1
    assert trace[0]["lastValidationIssues"][0]["path"] == "value"


def _lesson_candidate_json():
    blocks = [
        {
            "slot": slot,
            "kind": "text",
            "heading": f"正文块 {slot}",
            "content": "这一正文块完整解释当前目标的机制、判断依据与适用边界，并为测验提供直接证据。学习者可以据此复述因果链并检查反例。",
        }
        for slot in (
            "T1_CORE",
            "T2_CORE",
            "T3_CORE",
            "SHARED_EXAMPLE",
            "BOUNDARY",
            "PRACTICE",
            "SUMMARY",
        )
    ]
    questions = [
        {
            "target_slot": f"T{((index - 1) % 3) + 1}",
            "prompt": f"第 {index} 题：哪项符合正文教授的机制？",
            "options": [
                f"只看标题 {index}",
                f"依据机制判断 {index}",
                f"忽略边界 {index}",
            ],
            "correct": [1],
            "explanation": "正文要求依据机制和边界判断。",
        }
        for index in range(1, 5)
    ]
    return json.dumps(
        {
            "decision": "candidate",
            "replan_code": "",
            "replan_reason": "",
            "confidence": "high",
            "blocks": blocks,
            "questions": questions,
        },
        ensure_ascii=False,
    )


def _lesson_spec():
    return {
        "pipelineVersion": "lesson_generation_v2",
        "learningContractVersionId": "contract_1",
        "section": {"id": "section_1", "question": "为什么需要稳定绑定？"},
        "targets": [
            {
                "assessmentTargetId": f"target_{index}",
                "conceptRevisionId": f"concept_{index}",
                "objective": f"目标 {index}",
                "required": True,
            }
            for index in range(1, 4)
        ],
        "knowledgeContext": {
            "status": "ready",
            "claims": [
                {
                    "claimVersionId": f"claim_{index}",
                    "scope": {"conceptRevisionIds": [f"concept_{index}"]},
                }
                for index in range(1, 4)
            ],
        },
        "feedback": {},
    }


def test_openai_chat_lesson_v2_uses_exactly_one_physical_call():
    async def run():
        adapter, completions = await chat_adapter(
            [_lesson_candidate_json()],
            reasoning_mode="required",
        )
        result = await adapter.generate_lesson(_lesson_spec())
        return result, completions.calls, adapter.structured_trace()

    result, calls, trace = asyncio.run(run())
    assert len(result.blocks) == 7
    assert [block.block_key for block in result.blocks[:3]] == [
        "t1_core",
        "t2_core",
        "t3_core",
    ]
    assert result.questions[0].assessment_target_id == "target_1"
    assert result.questions[0].evidence_block_keys == ["t1_core"]
    assert {item.correct[0] for item in result.questions} == {0, 1, 2}
    assert [
        item.options[item.correct[0]]
        for item in result.questions
    ] == [f"依据机制判断 {index}" for index in range(1, 5)]
    assert len(calls) == 1
    assert calls[0]["extra_body"] == {"enable_thinking": False}
    assert calls[0]["max_tokens"] == 12000
    request_payload = json.loads(calls[0]["messages"][1]["content"])
    assert "段落之间保留一个空行" in calls[0]["messages"][0]["content"]
    assert "稳定并列项必须使用 bullet_list" in calls[0]["messages"][0]["content"]
    assert "有明确先后顺序的过程必须使用 ordered_steps" in calls[0]["messages"][0]["content"]
    assert "不合格候选整批失败且不会自动修复" in calls[0]["messages"][0]["content"]
    assert "若两个以上选项成立" in calls[0]["messages"][0]["content"]
    assert "选项 1/2/3/4" in calls[0]["messages"][0]["content"]
    assert [
        item["allowedClaimVersionIds"]
        for item in request_payload["serverSlotPlan"]["targetSlots"]
    ] == [["claim_1"], ["claim_2"], ["claim_3"]]
    assert request_payload["serverSlotPlan"]["optionalBlockSlots"] == ["TRANSITION"]
    assert trace[0]["schema"] == "GeneratedLessonSlotCandidate"
    assert trace[0]["attempts"] == 1
    assert trace[0]["repairAttempts"] == 0


def test_openai_chat_lesson_v2_does_not_repair_an_invalid_candidate():
    async def run():
        adapter, completions = await chat_adapter(['{"decision":"candidate"}'])
        with pytest.raises(AiError) as raised:
            await adapter.generate_lesson(_lesson_spec())
        return raised.value, completions.calls, adapter.structured_trace()

    error, calls, trace = asyncio.run(run())
    assert error.code == "AI_STRUCTURED_OUTPUT_INVALID"
    assert len(calls) == 1
    assert trace[0]["attempts"] == 1
    assert trace[0]["repairAttempts"] == 0


def test_openai_chat_lesson_v2_rejects_plan_external_slots_without_retry():
    payload = json.loads(_lesson_candidate_json())
    payload["blocks"][2]["slot"] = "T4_CORE"

    async def run():
        adapter, completions = await chat_adapter(
            [json.dumps(payload, ensure_ascii=False)]
        )
        with pytest.raises(AiError) as raised:
            await adapter.generate_lesson(_lesson_spec())
        return raised.value, completions.calls

    error, calls = asyncio.run(run())
    assert error.code == "AI_STRUCTURED_OUTPUT_INVALID"
    assert len(calls) == 1


def test_openai_chat_rejects_empty_content_without_invoking_repair():
    async def run():
        adapter, completions = await chat_adapter(
            ["", "", ""]
        )
        with pytest.raises(AiError) as raised:
            await adapter._parse(
                StructuredAnswer,
                "Return JSON.",
                {"question": "test"},
                100,
            )
        return raised.value, completions.calls, adapter.structured_trace()

    error, calls, trace = asyncio.run(run())

    assert error.code == "AI_EMPTY_RESPONSE"
    assert len(calls) == 3
    assert [item["max_tokens"] for item in calls] == [100, 200, 400]
    assert trace[0]["outcome"] == "failed"
    assert trace[0]["repairAttempts"] == 0
    assert trace[0]["tokenBudgets"] == [100, 200, 400]


def test_openai_chat_doubles_budget_after_incomplete_json_provider_abort():
    async def run():
        adapter, completions = await chat_adapter([
            RuntimeError(
                "Model output became abnormal; partial output may be incomplete or invalid JSON"
            ),
            '{"value":"ok"}',
        ])
        answer = await adapter._parse(
            StructuredAnswer,
            "Return JSON.",
            {"question": "test"},
            100,
        )
        return answer, completions.calls, adapter.structured_trace()

    answer, calls, trace = asyncio.run(run())

    assert answer.value == "ok"
    assert [item["max_tokens"] for item in calls] == [100, 200]
    assert trace[0]["tokenBudgets"] == [100, 200]
    assert trace[0]["repairAttempts"] == 0


def test_openai_chat_repairs_semantically_incomplete_remediation():
    bad = json.dumps(
        {
            "confidence": "high",
            "sources": [
                {
                    "title": "官方来源",
                    "url": "https://example.com/reference",
                    "kind": "official",
                    "version": "2026-08-03",
                }
            ],
            "blocks": [
                {
                    "kind": "text",
                    "role": "mechanism",
                    "heading": "换个角度理解三要素",
                    "content": "把信息可视化想象成一次",
                    "source_indexes": [0],
                }
            ],
        },
        ensure_ascii=False,
    )
    good_content = (
        "把信息可视化想象成一次有目的的翻译：原始数据是待翻译的事实，视觉编码是把属性转换为位置、长度和颜色的规则，"
        "认知目标则规定读者最终要比较、发现或判断什么。三者必须形成闭环；缺少数据便没有内容，缺少编码便无法观察，"
        "缺少认知目标则只剩装饰。用同一份销售记录分别支持趋势判断和门店比较时，编码选择也会随目标改变。"
    )
    good = json.dumps(
        {
            "confidence": "high",
            "sources": [
                {
                    "title": "官方来源",
                    "url": "https://example.com/reference",
                    "kind": "official",
                    "version": "2026-08-03",
                }
            ],
            "blocks": [
                {
                    "kind": "text",
                    "role": "mechanism",
                    "heading": "换个角度理解三要素",
                    "content": good_content,
                    "source_indexes": [0],
                }
            ],
        },
        ensure_ascii=False,
    )

    async def run():
        adapter, completions = await chat_adapter([bad, good])
        answer = await adapter._parse(
            GeneratedRemediationContent,
            "生成完整补救内容。",
            {"section": "test"},
            2600,
        )
        return answer, completions.calls, adapter.structured_trace()

    answer, calls, trace = asyncio.run(run())

    assert answer.blocks[0].content == good_content
    assert [item["max_tokens"] for item in calls] == [2600, 5200]
    assert "JSON 结构修复器" in calls[1]["messages"][0]["content"]
    assert trace[0]["repairAttempts"] == 1
    assert trace[0]["tokenBudgets"] == [2600, 5200]


def test_remediation_rejects_an_incomplete_markdown_table():
    with pytest.raises(ValidationError, match="markdown table is incomplete"):
        GeneratedRemediationContent.model_validate(
            {
                "confidence": "high",
                "sources": [
                    {
                        "title": "官方来源",
                        "url": "https://example.com/reference",
                        "kind": "official",
                        "version": "2026-08-03",
                    }
                ],
                "blocks": [
                    {
                        "kind": "table",
                        "role": "practice",
                        "heading": "三要素速查对照表",
                        "content": (
                            "| 要素 | 检查问题 |\n|---|---|\n"
                            "| 抽象数据 | 这份数据在现实中有没有对应事实，是否足以支持当前判断目标"
                        ),
                        "source_indexes": [0],
                    }
                ],
            }
        )


def test_regeneration_with_prior_questions_uses_full_lesson_contract():
    async def run():
        adapter, _completions = await chat_adapter([])
        adapter.capabilities = ProviderCapabilities(
            protocol="openai",
            api_mode="chat_completions",
            structured_output=True,
            streaming=True,
            reasoning_mode="required",
        )
        calls = []
        prior = [
            {
                "prompt": f"旧题 {index}",
                "options": ["A", "B", "C"],
                "correct": [0],
                "core": index == 0,
                "objective": f"目标 {index}",
                "explanation": "旧解释",
                "difficulty": "standard",
            }
            for index in range(4)
        ]

        async def fake_parse(schema, prompt, payload, tokens):
            calls.append((schema, prompt, payload, tokens))
            if schema is GeneratedContent:
                return GeneratedContent.model_validate({
                    "confidence": "high",
                    "sources": [{
                        "title": "官方来源",
                        "url": "https://example.com/reference",
                        "kind": "official",
                        "version": "2026-08-02",
                    }],
                    "blocks": [
                        {
                            "kind": "text",
                            "role": role,
                            "heading": f"标题 {index}",
                            "content": f"正文 {index}。",
                            "source_indexes": [0],
                        }
                        for index, role in enumerate(
                            ["conclusion", "mechanism", "example", "boundary", "practice"],
                            1,
                        )
                    ],
                })
            assert schema is GeneratedQuiz
            return GeneratedQuiz.model_validate({
                "questions": [
                    {
                        "prompt": f"新题 {index}",
                        "options": ["A", "B", "C"],
                        "correct": [1],
                        "core": False,
                        "objective": f"会被旧目标覆盖 {index}",
                        "explanation": "新解释",
                        "difficulty": "standard",
                    }
                    for index in range(4)
                ]
            })

        adapter._parse = fake_parse
        lesson = await adapter.lesson(
            {"id": "section_test", "rejectedSourceUrls": []},
            [],
            prior,
        )
        return lesson, calls, prior

    lesson, calls, prior = asyncio.run(run())

    assert calls[0][0] is GeneratedContent
    assert calls[0][3] == 12000
    assert calls[1][3] == 3600
    assert "数量必须与 prior_questions 完全一致" in calls[1][1]
    assert "不得让 core=true 的题只依赖这部分内容" in calls[1][1]
    assert len(lesson.blocks) == 5
    assert [item.objective for item in lesson.questions] == [
        item["objective"] for item in prior
    ]


def test_source_repair_uses_indexed_patch_and_preserves_other_blocks():
    async def run():
        adapter, _completions = await chat_adapter([])
        adapter.capabilities = ProviderCapabilities(
            protocol="openai",
            api_mode="chat_completions",
            structured_output=True,
            streaming=True,
            reasoning_mode="required",
        )
        content = GeneratedContent.model_validate({
            "confidence": "high",
            "sources": [
                {"title": "A", "url": "https://example.com/a", "kind": "official", "version": "1"},
                {"title": "B", "url": "https://example.com/b", "kind": "official", "version": "1"},
            ],
            "blocks": [
                {
                    "kind": "text",
                    "role": role,
                    "heading": f"标题 {index}",
                    "content": f"正文 {index}。",
                    "source_indexes": [index % 2],
                }
                for index, role in enumerate(
                    ["conclusion", "mechanism", "example", "boundary", "practice"]
                )
            ],
        })
        calls = []

        async def fake_parse(schema, prompt, payload, tokens):
            calls.append((schema, prompt, payload, tokens))
            return GeneratedSourceRepair.model_validate({
                "replacements": [{
                    "source_index": 1,
                    "source": {
                        "title": "B2",
                        "url": "https://replacement.example.org/b2",
                        "kind": "official",
                        "version": "2",
                    },
                    "blocks": [{
                        "block_index": 1,
                        "heading": "修正标题",
                        "content": "修正正文。",
                    }],
                }],
            })

        adapter._parse = fake_parse
        repaired = await adapter.repair_lesson_sources(
            {
                "rejectedSourceUrls": ["https://example.com/b"],
                "rejectedSourceHosts": ["example.com"],
            },
            [],
            content,
            [{"url": "https://example.com/b", "statusCode": 404}],
        )
        return content, repaired, calls

    original, repaired, calls = asyncio.run(run())

    assert calls[0][0] is GeneratedSourceRepair
    assert calls[0][2]["failed_source_indexes"] == [1]
    assert calls[0][2]["allowed_block_indexes"] == {1: [1, 3]}
    assert repaired.sources[0] == original.sources[0]
    assert repaired.sources[1].url == "https://replacement.example.org/b2"
    assert repaired.blocks[0] == original.blocks[0]
    assert repaired.blocks[1].content == "修正正文。"
    assert repaired.blocks[3] == original.blocks[3]


def test_source_repair_rejects_a_new_path_on_a_failed_host():
    async def run():
        adapter, _ = await chat_adapter([])
        content = GeneratedContent.model_validate({
            "confidence": "high",
            "sources": [{
                "title": "Old",
                "url": "https://docs.example.com/2023/missing",
                "kind": "official",
                "version": "2023",
            }],
            "blocks": [{
                "kind": "text",
                "role": role,
                "heading": role,
                "content": f"{role}。",
                "source_indexes": [0],
            } for role in ["conclusion", "mechanism", "example", "boundary", "practice"]],
        })

        async def fake_parse(schema, prompt, payload, tokens):
            return GeneratedSourceRepair.model_validate({
                "replacements": [{
                    "source_index": 0,
                    "source": {
                        "title": "Guessed",
                        "url": "https://docs.example.com/2024/missing",
                        "kind": "official",
                        "version": "2024",
                    },
                    "blocks": [],
                }],
            })

        adapter._parse = fake_parse
        with pytest.raises(AiError) as raised:
            await adapter.repair_lesson_sources(
                {
                    "rejectedSourceUrls": ["https://docs.example.com/2023/missing"],
                    "rejectedSourceHosts": ["docs.example.com"],
                },
                [],
                content,
                [{"url": "https://docs.example.com/2023/missing", "statusCode": 404}],
            )
        return raised.value

    error = asyncio.run(run())
    assert error.code == "SOURCE_REPAIR_SCOPE_VIOLATION"


def test_openai_chat_fails_closed_after_repair_budget_is_exhausted():
    async def run():
        adapter, completions = await chat_adapter(
            ['{"value":7}', '{"value":8}', '{"value":9}']
        )
        with pytest.raises(AiError) as raised:
            await adapter._parse(
                StructuredAnswer,
                "Return JSON.",
                {"question": "test"},
                100,
            )
        return raised.value, completions.calls, adapter.structured_trace()

    error, calls, trace = asyncio.run(run())

    assert error.code == "AI_STRUCTURED_OUTPUT_INVALID"
    assert len(calls) == 3
    assert trace[0]["outcome"] == "failed"
    assert trace[0]["attempts"] == 3
    assert trace[0]["repairAttempts"] == 2
    assert len(trace[0]["invalidOutputDigests"]) == 3


def test_structured_repair_records_each_physical_provider_request():
    async def run():
        engine, sessions = build_database("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        recorder = AiUsageRecorder(sessions)
        adapter, _completions = await chat_adapter(
            ['{"value":7}', '{"value":"ok"}']
        )
        adapter.set_usage_recorder(recorder)
        answer = await adapter._parse(
            StructuredAnswer,
            "Return JSON.",
            {"question": "test"},
            100,
        )
        with sessions() as db:
            invocations = db.scalars(
                select(AiInvocation).order_by(AiInvocation.started_at)
            ).all()
            measurements = db.scalars(
                select(AiUsageMeasurement)
            ).all()
        engine.dispose()
        return answer, invocations, measurements

    answer, invocations, measurements = asyncio.run(run())

    assert answer.value == "ok"
    assert len(invocations) == 2
    assert all(item.operation == "structured_call" for item in invocations)
    assert all(item.status == "succeeded" for item in invocations)
    assert all(item.attribution_status == "legacy_unverified" for item in invocations)
    assert all(item.subject_user_id is None for item in invocations)
    assert [item.input_tokens for item in measurements] == [3, 3]
    assert [item.output_tokens for item in measurements] == [2, 2]


def test_provider_connection_error_with_none_code_is_not_masked_by_metering():
    class FakeConnectionError(RuntimeError):
        code = None

    async def run():
        engine, sessions = build_database("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        recorder = AiUsageRecorder(sessions)
        adapter, _completions = await chat_adapter([FakeConnectionError("offline")])
        adapter.set_usage_recorder(recorder)
        with pytest.raises(AiError) as raised:
            await adapter._parse(
                StructuredAnswer,
                "Return JSON.",
                {"question": "test"},
                100,
            )
        with sessions() as db:
            invocation = db.scalar(select(AiInvocation))
        engine.dispose()
        return raised.value, invocation, adapter.structured_trace()

    error, invocation, trace = asyncio.run(run())

    assert error.code == "AI_PROVIDER_UNAVAILABLE"
    assert invocation.status == "failed"
    assert invocation.error_code == "FakeConnectionError"
    assert trace[0]["outcome"] == "provider_failed"


def test_missing_usage_is_explicit_instead_of_becoming_zero():
    engine, sessions = build_database("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    recorder = AiUsageRecorder(sessions)

    invocation_id = recorder.start(
        provider="openai",
        api_mode="responses",
        model="test-model",
        operation="connection_check",
        attribution_status="system",
    )
    recorder.succeed(invocation_id, None)

    with sessions() as db:
        invocation = db.get(AiInvocation, invocation_id)
        measurement_count = len(
            db.scalars(select(AiUsageMeasurement)).all()
        )
    engine.dispose()

    assert invocation.status == "succeeded"
    assert invocation.usage_status == "missing"
    assert invocation.attribution_status == "system"
    assert invocation.subject_user_id is None
    assert measurement_count == 0


def test_usage_recorder_attributes_invocation_to_verified_principal():
    engine, sessions = build_database("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    recorder = AiUsageRecorder(sessions)
    with sessions() as db:
        db.add(User(id="user_a", name="A"))
        db.commit()

    principal = Principal(
        actor_kind="user",
        actor_id="user_a",
        subject_user_id="user_a",
        session_id="session_a",
    )
    with recorder.attributed(principal):
        invocation_id = recorder.start(
            provider="openai",
            api_mode="responses",
            model="test-model",
            operation="section_generation",
        )
        recorder.succeed(invocation_id, None)

    with sessions() as db:
        invocation = db.get(AiInvocation, invocation_id)
        assert invocation.attribution_status == "verified"
        assert invocation.actor_kind == "user"
        assert invocation.actor_id == "user_a"
        assert invocation.subject_user_id == "user_a"
    engine.dispose()


def test_anthropic_adapter_uses_messages_path_and_validates_schema():
    paths = []

    def handler(request: httpx.Request):
        paths.append(request.url.path)
        body = json.loads(request.content)
        assert body["model"] == "qwen-test"
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": '{"value":"ok"}'}
                ],
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
        )

    async def run():
        adapter = AnthropicAdapter(
            "test-key",
            "qwen-test",
            "https://workspace.example/apps/anthropic",
            capabilities=ProviderCapabilities(
                protocol="anthropic",
                api_mode="messages",
                structured_output=True,
                streaming=True,
                reasoning_mode="optional",
            ),
            transport=httpx.MockTransport(handler),
        )
        try:
            answer = await adapter._parse(
                StructuredAnswer,
                "Return JSON.",
                {"question": "test"},
                100,
            )
            assert answer.value == "ok"
            assert adapter.input_tokens == 3
            assert adapter.output_tokens == 2
        finally:
            await adapter.close()

    asyncio.run(run())
    assert paths == ["/apps/anthropic/v1/messages"]


def test_anthropic_adapter_repairs_invalid_schema_before_returning():
    calls = []
    outputs = iter(['{"value":7}', '{"value":"fixed"}'])

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": next(outputs)}],
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
        )

    async def run():
        adapter = AnthropicAdapter(
            "test-key",
            "qwen-test",
            "https://workspace.example/apps/anthropic",
            transport=httpx.MockTransport(handler),
        )
        try:
            answer = await adapter._parse(
                StructuredAnswer,
                "Return JSON.",
                {"question": "test"},
                100,
            )
            return answer, adapter.structured_trace()
        finally:
            await adapter.close()

    answer, trace = asyncio.run(run())

    assert answer.value == "fixed"
    assert len(calls) == 2
    assert "JSON 结构修复器" in calls[1]["system"]
    assert trace[0]["repairAttempts"] == 1


def test_anthropic_adapter_streams_text_deltas():
    def handler(_request: httpx.Request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                'event: content_block_delta\n'
                'data: {"delta":{"type":"text_delta","text":"你好"}}\n\n'
                'event: content_block_delta\n'
                'data: {"delta":{"type":"text_delta","text":"，世界"}}\n\n'
            ).encode(),
        )

    async def run():
        adapter = AnthropicAdapter(
            "test-key",
            "qwen-test",
            "https://workspace.example/apps/anthropic",
            transport=httpx.MockTransport(handler),
        )
        try:
            return [
                delta
                async for delta in adapter.answer_stream(
                    {"question": "test"}
                )
            ]
        finally:
            await adapter.close()

    assert asyncio.run(run()) == ["你好", "，世界"]
