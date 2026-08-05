import asyncio

from .contracts import (
    AskMeTurn,
    ChoiceQuestion,
    ClassifiedAnswer,
    ContentBlock,
    GeneratedChapter,
    GeneratedContent,
    GeneratedLesson,
    GeneratedNote,
    GeneratedPlan,
    GeneratedQuiz,
    GeneratedRemediationContent,
    GeneratedRemediationLesson,
    GeneratedSectionOutline,
    LessonAlignmentReview,
    PlanBook,
    PlanChapter,
    PlanMilestone,
    PlanMilestoneCriterion,
    ReplannedBook,
    ReplannedChapter,
    Source,
)
from .port import ProviderCapabilities


class LocalDemoAdapter:
    """Deterministic, clearly labelled fallback that keeps the full learning loop demonstrable."""

    configured = False
    model = "local-demo-v1"
    staged_lesson_generation = True
    capabilities = ProviderCapabilities(
        protocol="openai",
        api_mode="responses",
        structured_output=True,
        streaming=True,
        reasoning_mode="disabled",
    )

    async def close(self):
        return None

    async def plan(self, request, memory):
        topic = request["topic"]
        return GeneratedPlan(
            series_title=f"{topic}：本地演示学习路径",
            rationale="未配置 API Key，使用可完整操作的本地示例内容。",
            assumptions=["内容用于产品流程演示，不替代真实 AI 个性化教材"],
            confidence="medium",
            books=[
                PlanBook(
                    title=f"{topic} 基础机制",
                    topic=topic,
                    description="用两个章节演示生成、答疑、验证、口试与笔记闭环。",
                    estimated_minutes=240,
                    chapters=[
                        PlanChapter(title="核心对象", objective=f"解释 {topic} 的核心对象及关系"),
                        PlanChapter(title="边界与实践", objective=f"识别 {topic} 的适用边界并完成实践"),
                    ],
                ),
                PlanBook(
                    title=f"{topic} 迁移实践",
                    topic=topic,
                    description="进入第二本书，验证跨书记忆和迁移。",
                    estimated_minutes=240,
                    chapters=[
                        PlanChapter(title="迁移场景", objective=f"把 {topic} 机制迁移到新场景"),
                        PlanChapter(title="综合排障", objective=f"综合定位 {topic} 的边界问题"),
                    ],
                ),
            ],
            milestones=[
                PlanMilestone(
                    title=f"建立 {topic} 的核心理解",
                    outcome=f"能够解释 {topic} 的核心对象与关系",
                    criteria=[
                        PlanMilestoneCriterion(
                            statement=f"解释 {topic} 的核心对象及关系",
                            book_position=1,
                            chapter_position=1,
                        ),
                    ],
                ),
                PlanMilestone(
                    title=f"判断 {topic} 的边界并迁移",
                    outcome=f"能够识别 {topic} 的适用边界，并把机制用于新场景",
                    criteria=[
                        PlanMilestoneCriterion(
                            statement=f"识别 {topic} 的适用边界并完成实践",
                            book_position=1,
                            chapter_position=2,
                        ),
                        PlanMilestoneCriterion(
                            statement=f"把 {topic} 机制迁移到新场景",
                            book_position=2,
                            chapter_position=1,
                        ),
                    ],
                ),
                PlanMilestone(
                    title=f"综合诊断 {topic} 的异常",
                    outcome=f"能够在综合场景中定位 {topic} 的边界问题",
                    criteria=[
                        PlanMilestoneCriterion(
                            statement=f"综合定位 {topic} 的边界问题",
                            book_position=2,
                            chapter_position=2,
                        ),
                    ],
                ),
            ],
        )

    async def chapter(self, request, memory):
        return GeneratedChapter(
            sections=[
                GeneratedSectionOutline(title=f"{request['title']}：问题 {index}", question=f"本节如何解决递进问题 {index}？", objectives=[f"{request['objective']}（目标 {index}）"])
                for index in range(1, 4)
            ]
        )

    async def lesson_content(self, request, memory, prior_questions=None):
        source = Source(title="Python 官方教程（本地流程示例引用）", url="https://docs.python.org/3/tutorial/", kind="official", version="3.12")
        roles = ["conclusion", "mechanism", "example", "boundary", "practice"]
        blocks = [
            ContentBlock(
                kind="text",
                role=role,
                heading=f"{role}：{request['title']}",
                content=(
                    f"这是 {request['title']} 的{role}演示内容。它会先说明本节概念与问题之间的关系，"
                    "再用一个可观察的例子解释判断依据，并指出容易混淆的边界。"
                    "学习者可以据此复述机制、检查反例，并通过后续选择题验证自己是否真正理解。"
                ),
                source_indexes=[0],
            )
            for role in roles
        ]
        schema = (
            GeneratedRemediationContent
            if request.get("remediationStrategy")
            else GeneratedContent
        )
        return schema(
            confidence="medium",
            sources=[source],
            blocks=blocks,
        )

    async def repair_lesson_sources(
        self,
        request,
        memory,
        content,
        failed_sources,
        prior_questions=None,
    ):
        return content

    async def lesson_quiz(self, request, content, prior_questions=None):
        generation = 2 if prior_questions else 1
        objectives = request.get("objectives") or [request["question"]]
        question_count = len(prior_questions) if prior_questions else 5
        questions = [
            ChoiceQuestion(
                prompt=f"第 {generation} 套：关于目标 {index + 1}，哪项符合本节结论？",
                options=[f"干扰项 {generation}-A-{index}", f"正确项 {generation}-B-{index}", f"干扰项 {generation}-C-{index}"],
                correct=[1],
                core=(
                    prior_questions[index].get("core", False)
                    if prior_questions
                    else index == 0
                ),
                objective=(
                    prior_questions[index]["objective"]
                    if prior_questions
                    else objectives[index % len(objectives)]
                ),
                explanation="正确项与本地演示正文中的机制描述一致。",
            )
            for index in range(question_count)
        ]
        return GeneratedQuiz(questions=questions)

    async def lesson(self, request, memory, prior_questions=None):
        content = await self.lesson_content(request, memory, prior_questions)
        quiz = await self.lesson_quiz(request, content, prior_questions)
        schema = (
            GeneratedRemediationLesson
            if request.get("remediationStrategy")
            else GeneratedLesson
        )
        return schema(
            **content.model_dump(),
            questions=quiz.questions,
        )

    async def review_lesson_alignment(self, request, content, quiz):
        return LessonAlignmentReview(
            allowed=True,
            issues=[],
            covered_objectives=request.get("objectives") or [request["question"]],
        )

    async def answer(self, request):
        requested = request.get("requestedThreadId")
        return ClassifiedAnswer(
            relation="follow_up" if requested else "new_question",
            thread_id=request.get("newThreadId") or requested,
            answer="这是本地演示答疑：请回到锚定段落，对照机制、前提与边界逐项检查。",
            thread_summary="围绕锚定段落核对机制和边界",
        )

    async def answer_stream(self, request):
        for chunk in ["这是本地演示答疑：", "请回到锚定段落，", "对照机制、前提与边界", "逐项检查。"]:
            await asyncio.sleep(0.03)
            yield chunk

    async def note(self, request):
        return GeneratedNote(
            solved_question=request["section"]["question"],
            core_mechanism=["用结论、机制、例子和边界形成可验证解释"],
            personal_gaps=["根据错题与答疑继续补充"],
            boundaries=["本地内容只用于流程演示"],
            practice_checks=["完成章末实践并保存证据"],
            sources=["Python 官方教程（流程示例）"],
            unresolved=[],
        )

    async def ask_me(self, request):
        answered = bool(request.get("previousAnswer"))
        return AskMeTurn(
            dimension=request["dimension"],
            prompt=f"请用自己的话回答 {request['dimension']} 维度的问题，并给出一个可验证例子。",
            evaluation="partial" if answered else "not_evaluated",
            rationale="演示评估仅检查是否提交了自主回答。" if answered else "",
        )

    async def replan_book(self, request, memory):
        future = request.get("future_chapters") or [{"title": "未来章节", "objective": "完成迁移验证"}]
        return ReplannedBook(rationale="本地演示按现有未来章节重建顺序", chapters=[ReplannedChapter(**item) for item in future])
