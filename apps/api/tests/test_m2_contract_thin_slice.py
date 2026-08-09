from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.ai.contracts import (
    GeneratedChapter,
    GeneratedPlan,
    GeneratedSectionOutline,
    PlanBook,
    PlanChapter,
    PlanMilestone,
    PlanMilestoneCriterion,
)
from app.infrastructure.tables import (
    ContentBlockVersion,
    ContentVersion,
    GovernanceDecisionSnapshot,
    KnowledgeGap,
    LearningContractVersion,
    LearningDecisionSnapshot,
    LearningMissionVersion,
    LearningRunSectionBinding,
    QuizAttempt,
    QuizSet,
    Section,
    SectionProgress,
)
from app.main import create_app
from app.services.source_verifier import AcceptingSourceVerifier

from test_vertical_slice import FakeAi, wait_for_task


class ContractThinSliceAi(FakeAi):
    async def plan(self, request, memory):
        chapters = [
            PlanChapter(
                title=f"M2 契约薄切片第 {index} 章",
                objective=f"完成依赖路径第 {index} 阶段的可信学习链路",
            )
            for index in range(1, 3)
        ]
        return GeneratedPlan(
            series_title="M2 契约薄切片工程验证",
            rationale="用一个语义连续的三目标样本验证版本与证据闭环；节数不是课程完整性配额。",
            assumptions=[],
            confidence="high",
            books=[
                PlanBook(
                    title="M2 契约工程闭环",
                    topic="M2 acceptance",
                    description="覆盖使命、合同、版本冻结、治理、答题与决策的最小样本。",
                    estimated_minutes=120,
                    chapters=chapters,
                )
            ],
            milestones=[
                PlanMilestone(
                    title="冻结契约",
                    outcome="形成稳定学习契约",
                    criteria=[
                        PlanMilestoneCriterion(
                            statement="目标一绑定冻结契约",
                            book_position=1,
                            chapter_position=1,
                        )
                    ],
                ),
                PlanMilestone(
                    title="形成观察",
                    outcome="形成可回放学习证据",
                    criteria=[
                        PlanMilestoneCriterion(
                            statement="目标二形成不可变观察",
                            book_position=1,
                            chapter_position=1,
                        )
                    ],
                ),
                PlanMilestone(
                    title="闭合路径",
                    outcome="完成薄切片推进决策",
                    criteria=[
                        PlanMilestoneCriterion(
                            statement="目标三形成可解释推进决策",
                            book_position=1,
                            chapter_position=2,
                        )
                    ],
                ),
            ],
        )

    async def chapter(self, request, memory):
        return GeneratedChapter(
            sections=[
                GeneratedSectionOutline(
                    title=f"{request['title']}：第 {index} 节",
                    question=f"{request['objective']} 的递进问题 {index} 是什么？",
                    objectives=[f"{request['objective']}（目标 {index}）"],
                )
                for index in range(1, 3)
            ]
        )


def test_m2_contract_thin_slice_engineering_chain_is_complete():
    with TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            ContractThinSliceAi(),
            AcceptingSourceVerifier(),
        )
    ) as client:
        created = client.post(
            "/api/plans",
            json={
                "shelfId": "shelf_technology",
                "topic": "M2 契约薄切片",
                "role": "工程验证者",
                "experience": "熟悉系统测试",
                "purpose": "验证三目标可信链路",
                "depth": "deep",
                "details": "只使用确定性本地 AI fixture",
            },
        )
        assert created.status_code == 201, created.json()
        series = created.json()
        assert wait_for_task(
            client,
            series["initializationTask"]["taskId"],
        )["status"] == "succeeded"

        chapter_ids = [
            chapter["id"]
            for chapter in client.get(f"/api/series/{series['id']}").json()[
                "books"
            ][0]["chapters"]
        ]
        completed_ids = []
        for chapter_id in chapter_ids:
            chapter_response = client.post(f"/api/chapters/{chapter_id}/generate")
            assert chapter_response.status_code == 200, chapter_response.json()
            chapter = chapter_response.json()
            assert len(chapter["sections"]) == 2
            for summary in chapter["sections"]:
                generated = client.post(f"/api/sections/{summary['id']}/generate")
                assert generated.status_code == 200, generated.json()
                opened = client.post(f"/api/sections/{summary['id']}/open")
                assert opened.status_code == 200, opened.json()
                section = opened.json()
                assert section["versionBinding"]
                assert section["quiz"]["governance"]["allowed"] is True
                assert section["quiz"]["governance"]["assessmentEligible"] is True
                submitted = client.post(
                    f"/api/sections/{summary['id']}/quiz",
                    json={
                        "quizSetId": section["quiz"]["id"],
                        "answers": [[1] for _ in section["quiz"]["questions"]],
                    },
                )
                assert submitted.status_code == 200, submitted.json()
                result = submitted.json()
                assert result["passed"] is True
                for task in result["workflowTasks"]:
                    assert wait_for_task(client, task["taskId"])["status"] == "succeeded"
                completed_ids.append(summary["id"])

        assert len(completed_ids) == len(set(completed_ids)) == 4
        final = client.get(f"/api/series/{series['id']}").json()
        assert final["books"][0]["status"] == "completed"

        with client.app.state.sessions() as db:
            assert db.scalar(select(func.count()).select_from(Section)) == 4
            assert db.scalar(
                select(func.count()).select_from(SectionProgress).where(
                    SectionProgress.status == "completed"
                )
            ) == 4
            assert db.scalar(
                select(func.count()).select_from(LearningMissionVersion)
            ) == 1
            assert db.scalar(
                select(func.count()).select_from(LearningContractVersion)
            ) == 4
            assert db.scalar(
                select(func.count()).select_from(LearningRunSectionBinding)
            ) == 4
            assert db.scalar(select(func.count()).select_from(ContentVersion)) == 4
            assert db.scalar(select(func.count()).select_from(QuizSet)) == 4
            assert db.scalar(select(func.count()).select_from(QuizAttempt)) == 4
            assert db.scalar(
                select(func.count()).select_from(ContentBlockVersion)
            ) == 20
            assert db.scalar(
                select(func.count()).select_from(GovernanceDecisionSnapshot)
            ) == 12
            # conclusion, mechanism and boundary each carry an explicit
            # claim-level gap per generated section.
            assert db.scalar(select(func.count()).select_from(KnowledgeGap)) == 12
            assert db.scalar(
                select(func.count()).select_from(LearningDecisionSnapshot).where(
                    LearningDecisionSnapshot.decision_kind == "assessment_gate"
                )
            ) == 4
            assert db.scalar(
                select(func.count()).select_from(LearningDecisionSnapshot).where(
                    LearningDecisionSnapshot.decision_kind == "progression"
                )
            ) == 4
