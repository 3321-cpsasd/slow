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


class TwentySectionAi(FakeAi):
    async def plan(self, request, memory):
        chapters = [
            PlanChapter(
                title=f"M2 验收章 {index}",
                objective=f"完成 M2 能力目标 {index}",
            )
            for index in range(1, 6)
        ]
        return GeneratedPlan(
            series_title="M2 二十节工程验收",
            rationale="用五章、每章四节验证跨节版本与证据闭环。",
            assumptions=[],
            confidence="high",
            books=[
                PlanBook(
                    title="M2 工程闭环",
                    topic="M2 acceptance",
                    description="覆盖使命、合同、版本冻结、治理、答题与决策。",
                    estimated_minutes=1200,
                    chapters=chapters,
                )
            ],
            milestones=[
                PlanMilestone(
                    title="建立合同",
                    outcome="前三章形成稳定学习合同",
                    criteria=[
                        PlanMilestoneCriterion(
                            statement=chapters[0].objective,
                            book_position=1,
                            chapter_position=1,
                        )
                    ],
                ),
                PlanMilestone(
                    title="积累证据",
                    outcome="跨章积累可回放证据",
                    criteria=[
                        PlanMilestoneCriterion(
                            statement=chapters[2].objective,
                            book_position=1,
                            chapter_position=3,
                        )
                    ],
                ),
                PlanMilestone(
                    title="闭合路径",
                    outcome="完成整本书的推进决策",
                    criteria=[
                        PlanMilestoneCriterion(
                            statement=chapters[4].objective,
                            book_position=1,
                            chapter_position=5,
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
                for index in range(1, 5)
            ]
        )


def test_m2_twenty_section_engineering_loop_is_complete():
    with TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            TwentySectionAi(),
            AcceptingSourceVerifier(),
        )
    ) as client:
        created = client.post(
            "/api/plans",
            json={
                "shelfId": "shelf_technology",
                "topic": "M2 工程验收",
                "role": "工程验证者",
                "experience": "熟悉系统测试",
                "purpose": "验证二十节学习闭环",
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
            assert len(chapter["sections"]) == 4
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

        assert len(completed_ids) == len(set(completed_ids)) == 20
        final = client.get(f"/api/series/{series['id']}").json()
        assert final["books"][0]["status"] == "completed"

        with client.app.state.sessions() as db:
            assert db.scalar(select(func.count()).select_from(Section)) == 20
            assert db.scalar(
                select(func.count()).select_from(SectionProgress).where(
                    SectionProgress.status == "completed"
                )
            ) == 20
            assert db.scalar(
                select(func.count()).select_from(LearningMissionVersion)
            ) == 1
            assert db.scalar(
                select(func.count()).select_from(LearningContractVersion)
            ) == 20
            assert db.scalar(
                select(func.count()).select_from(LearningRunSectionBinding)
            ) == 20
            assert db.scalar(select(func.count()).select_from(ContentVersion)) == 20
            assert db.scalar(select(func.count()).select_from(QuizSet)) == 20
            assert db.scalar(select(func.count()).select_from(QuizAttempt)) == 20
            assert db.scalar(
                select(func.count()).select_from(ContentBlockVersion)
            ) == 100
            assert db.scalar(
                select(func.count()).select_from(GovernanceDecisionSnapshot)
            ) == 60
            assert db.scalar(select(func.count()).select_from(KnowledgeGap)) == 40
            assert db.scalar(
                select(func.count()).select_from(LearningDecisionSnapshot).where(
                    LearningDecisionSnapshot.decision_kind == "assessment_gate"
                )
            ) == 20
            assert db.scalar(
                select(func.count()).select_from(LearningDecisionSnapshot).where(
                    LearningDecisionSnapshot.decision_kind == "progression"
                )
            ) == 20
