import asyncio

from .contracts import (
    AskMeDiscussionTurn,
    AskMeTurn,
    ChoiceQuestion,
    ClaimSupportReview,
    ClassifiedAnswer,
    ContentBlock,
    GeneratedChapter,
    GeneratedContent,
    GeneratedLesson,
    GeneratedLessonBlock,
    GeneratedLessonCandidate,
    GeneratedLessonFeedbackReplacement,
    GeneratedLessonQuestion,
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
    TeachingBlueprint,
    TeachingBlueprintBlock,
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

    async def teaching_blueprint(self, request, memory):
        preferences = (
            request.get("generationContext", {})
            .get("learner", {})
            .get("preferences", {})
        )
        formats = preferences.get("formatPreferences") or []
        return TeachingBlueprint(
            narrative_thread="从一个可观察的问题出发，建立判断机制，再用同一场景检查边界与迁移。",
            opening_move="先给出一个需要判断的真实问题，让学习者带着问题进入机制。",
            recurring_example="持续使用同一个最小场景，对照输入、机制、结果和失败边界。",
            core_model="对象在约束下经过机制产生可观察结果，边界条件决定结论何时不成立。",
            recap_prompt="不用术语，复述对象、机制、结果和一个失败边界。",
            preference_applications=[
                f"已记录表现形式偏好 {item}；本地演示内容仍使用文字以避免伪造该形式。"
                for item in formats
            ],
            blocks=[
                TeachingBlueprintBlock(kind="text", role="conclusion", purpose="建立问题与答案的方向感", heading_intent="先解决眼前的问题"),
                TeachingBlueprintBlock(kind="text", role="mechanism", purpose="解释结果为何发生", heading_intent="沿着因果链向前走"),
                TeachingBlueprintBlock(kind="text", role="example", purpose="用贯穿场景观察机制", heading_intent="把机制放进一个场景"),
                TeachingBlueprintBlock(kind="text", role="boundary", purpose="识别结论失效条件", heading_intent="什么时候不能这样判断"),
                TeachingBlueprintBlock(kind="text", role="practice", purpose="复述并迁移核心模型", heading_intent="换一个场景再判断一次"),
            ],
        )

    async def generate_lesson(self, spec):
        targets = spec["targets"]
        blocks = []
        for index, target in enumerate(targets):
            blocks.append(
                GeneratedLessonBlock(
                    block_key=f"b{index + 1}",
                    kind="text",
                    role="core_instruction",
                    relation_to_anchor="core",
                    assessment_target_ids=[target["assessmentTargetId"]],
                    teaching_moves=["direct_explanation"],
                    reader_priority="essential",
                    heading=f"{spec['section']['title']}：演示说明 {index + 1}",
                    content=(
                        f"这是围绕“{spec['section']['question']}”生成的本地演示教材块。"
                        f"它用于教授目标“{target['objective']}”，说明判断机制、观察线索和适用边界。"
                        "学习者应能根据本段复述原因，并在后续题目中识别正确结论。"
                    ),
                )
            )
        for role, relation, move, case_kind in (
            ("mechanism", "mechanism", "explain_mechanism", ""),
            ("practice", "practice", "guided_practice", "hypothetical_example"),
        ):
            position = len(blocks) + 1
            blocks.append(
                GeneratedLessonBlock(
                    block_key=f"b{position}",
                    kind="text",
                    role=role,
                    relation_to_anchor=relation,
                    teaching_moves=[move],
                    case_kind=case_kind,
                    case_key=(f"demo_case_{position}" if case_kind else ""),
                    heading=f"{spec['section']['title']}：演示支持 {position}",
                    content=(
                        f"这一段围绕“{spec['section']['question']}”提供不参与新增考核目标的支持说明。"
                        "它帮助学习者连接核心依据、适用条件与后续练习，但不会扩大本节的验证范围。"
                    ),
                )
            )
        question_count = max(4, min(5, len(targets)))
        questions = []
        for index in range(question_count):
            target_index = index % len(targets)
            target = targets[target_index]
            questions.append(
                GeneratedLessonQuestion(
                    item_key=f"q{index + 1}",
                    assessment_target_id=target["assessmentTargetId"],
                    evidence_block_keys=[f"b{target_index + 1}"],
                    prompt=f"关于“{target['objective']}”，哪一项符合本节演示正文？",
                    options=["忽略机制直接猜测", "依据机制和边界进行判断", "把示例当作普遍定律"],
                    correct=[1],
                    explanation="正文要求同时依据机制、观察线索和适用边界判断。",
                )
            )
        return GeneratedLessonCandidate(
            confidence="medium",
            blocks=blocks,
            questions=questions,
            feedback_replacement=(
                GeneratedLessonFeedbackReplacement(
                    source_block_id=spec["feedback"]["blockId"],
                    replacement_block_key="b1",
                )
                if spec.get("feedback")
                else None
            ),
        )

    async def lesson_content(self, request, memory, prior_questions=None):
        source = Source(title="Python 官方教程（本地流程示例引用）", url="https://docs.python.org/3/tutorial/", kind="official", version="3.12")
        blueprint = request.get("teachingBlueprint", {})
        planned_blocks = blueprint.get("blocks") or [
            {"kind": "text", "role": role, "heading_intent": f"{role}：{request['title']}"}
            for role in ["conclusion", "mechanism", "example", "boundary", "practice"]
        ]
        blocks = [
            ContentBlock(
                kind=item.get("kind", "text"),
                role=item["role"],
                heading=item.get("heading_intent") or f"理解 {request['title']}",
                content=(
                    f"这是 {request['title']} 围绕贯穿场景展开的演示内容。它会先说明本节概念与问题之间的关系，"
                    "再用一个可观察的例子解释判断依据，并指出容易混淆的边界。"
                    "学习者可以据此复述机制、检查反例，并通过后续选择题验证自己是否真正理解。"
                ),
                source_indexes=[0],
                assessment_objectives=list(request.get("objectives") or [request["question"]]),
            )
            for item in planned_blocks
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
                claim_block_indexes=[] if prior_questions else [0],
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

    async def review_source_claim(self, request):
        return ClaimSupportReview(supported=False)

    async def answer(self, request):
        requested = request.get("requestedThreadId")
        mode_copy = (
            "先给出一句结论，再列出两个可立即检查的要点。"
            if request.get("dailyMode") == "fast"
            else "沿着结论、机制和边界完整说明。"
        )
        return ClassifiedAnswer(
            relation="follow_up" if requested else "new_question",
            thread_id=request.get("newThreadId") or requested,
            answer=f"这是本地演示答疑：{mode_copy}",
            thread_summary="围绕锚定段落核对机制和边界",
        )

    async def answer_stream(self, request):
        chunks = (
            ["这是本地演示答疑：", "先给出一句结论，", "再列出两个可立即检查的要点。"]
            if request.get("dailyMode") == "fast"
            else ["这是本地演示答疑：", "请回到锚定段落，", "对照机制、前提与边界", "逐项检查。"]
        )
        for chunk in chunks:
            await asyncio.sleep(0.03)
            yield chunk

    async def repair_stream(self, request):
        content = request.get("targetBlock", {}).get("content", "")
        for chunk in ["补救后的正文：", content]:
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

    async def ask_me_discussion(self, request):
        answer = str(request.get("previousAnswer", "")).strip()
        topic = request.get("currentTopic") or {}
        title = str(topic.get("title", "当前主题"))
        if len(answer) >= 45:
            evaluation = "strong"
            correct_points = ["回答给出了较完整的判断，并尝试说明依据。"]
            issues = []
            suggestions = ["再补充一个会让当前判断失效的反例。"]
            sufficient = "sufficient"
        elif len(answer) >= 16:
            evaluation = "partial"
            correct_points = ["回答已经触及当前主题的关键对象。"]
            issues = [{
                "kind": "evidence_insufficient",
                "answer_excerpt": answer[:80],
                "explanation": "目前给出了判断，但支撑判断的可观察依据还不够具体。",
            }]
            suggestions = ["补充一个可以被第三方验证的业务或技术信号。"]
            sufficient = "insufficient"
        else:
            evaluation = "weak"
            correct_points = []
            issues = [{
                "kind": "reasoning_gap",
                "answer_excerpt": answer[:80],
                "explanation": "回答还没有把结论和判断依据连接起来。",
            }]
            suggestions = ["先写出你的结论，再说明你依据的两个具体信号。"]
            sufficient = "insufficient"
        return AskMeDiscussionTurn(
            evaluation=evaluation,
            correct_points=correct_points,
            issues=issues,
            suggestions=suggestions,
            follow_up_prompt="什么证据会让你改变刚才的判断？",
            follow_up_purpose="检查判断依据是否稳定，并探测可能遗漏的边界。",
            topic_sufficiency=sufficient,
        )

    async def replan_book(self, request, memory):
        future = request.get("future_chapters") or [{"title": "未来章节", "objective": "完成迁移验证"}]
        return ReplannedBook(rationale="本地演示按现有未来章节重建顺序", chapters=[ReplannedChapter(**item) for item in future])
