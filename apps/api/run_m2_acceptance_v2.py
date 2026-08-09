"""Run the real-provider M2 v2 thin slice in an isolated SQLite database."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.application.service import DEMO_USER_ID
from app.evaluation.m2_runner import (
    M2AcceptanceRunner,
    M2FailureProbeRunner,
    M2HttpJourneyDriver,
    M2RunnerInputs,
    SqlAlchemyM2EvidenceAuditor,
)
from app.evaluation.runner import configured_provider, configured_provider_adapter
from app.main import build_provider_adapter, create_app
from app.modules.learning.rebuild import rebuild_user_projections
from app.services.attachment_storage import LocalAttachmentStorage


API_ROOT = Path(__file__).resolve().parent


def _source_tree_digest() -> str:
    roots = (
        API_ROOT / "app",
        API_ROOT / "migrations",
        API_ROOT / "tests",
        API_ROOT / "curriculum_baselines",
        API_ROOT / "knowledge_graph_slices",
        API_ROOT.parents[1] / "apps" / "web" / "src",
        API_ROOT.parents[1] / "docs" / "decisions",
    )
    paths = [API_ROOT / "run_m2_acceptance_v2.py"]
    for root in roots:
        paths.extend(
            item
            for item in root.rglob("*")
            if item.is_file() and "__pycache__" not in item.parts
        )
    digest = hashlib.sha256()
    workspace = API_ROOT.parents[1]
    for path in sorted(set(paths)):
        digest.update(str(path.relative_to(workspace)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _revision() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=API_ROOT.parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    head = result.stdout.strip() or "working-tree-unknown"
    return f"{head}+workspace-sha256:{_source_tree_digest()}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-path", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--code-revision", default="")
    parser.add_argument("--a1-evidence", action="append", required=True)
    parser.add_argument("--a2-evidence", action="append", required=True)
    parser.add_argument(
        "--knowledge-review-path",
        type=Path,
        default=(
            API_ROOT
            / "knowledge_graph_slices"
            / "pku_recursion_search_dp_v1_review_pending.json"
        ),
        help=(
            "Independent human review manifest. The checked-in pending manifest "
            "fails closed; pass an explicitly approved manifest to run M2-E."
        ),
    )
    args = parser.parse_args()

    database_path = args.database_path.resolve()
    output_dir = args.output_dir.resolve()
    if database_path.exists():
        raise SystemExit(
            f"refusing to overwrite acceptance database: {database_path}"
        )
    database_path.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("m2-v2-%Y%m%dT%H%M%SZ")

    config, capabilities = configured_provider()
    app_adapter = configured_provider_adapter()
    learner_adapter = build_provider_adapter(
        config["api_key"],
        config["provider_model"],
        config["base_url"],
        capabilities,
    )
    storage = LocalAttachmentStorage(
        output_dir / "evidence" / f"{run_id}-attachments",
        20 * 1024 * 1024,
    )
    app = create_app(
        f"sqlite+pysqlite:///{database_path}",
        ai=app_adapter,
        attachment_storage=storage,
    )
    loop = asyncio.new_event_loop()
    try:
        with TestClient(app) as client, client.app.state.sessions() as db:
            inputs = M2RunnerInputs(
                run_id=run_id,
                code_revision=args.code_revision or _revision(),
                baseline_package_path=(
                    API_ROOT
                    / "curriculum_baselines"
                    / "pku_cs_programming_practice_2025_v1.json"
                ),
                baseline_review_path=(
                    API_ROOT
                    / "curriculum_baselines"
                    / "pku_cs_programming_practice_2025_v1_review_20260809.json"
                ),
                knowledge_package_path=(
                    API_ROOT
                    / "knowledge_graph_slices"
                    / "pku_recursion_search_dp_v1.json"
                ),
                knowledge_review_path=args.knowledge_review_path.resolve(),
                historical_a1_evidence=tuple(args.a1_evidence),
                historical_a2_evidence=tuple(args.a2_evidence),
            )

            def answerer(section: dict, quiz: dict) -> list[list[int]]:
                answers: list[list[int]] = []
                for _attempt in range(3):
                    result = loop.run_until_complete(
                        learner_adapter.evaluation_quiz_answers(
                            {
                                "section": {
                                    "id": section["id"],
                                    "title": section["title"],
                                    "question": section["question"],
                                    "content": section["content"],
                                },
                                "questions": quiz["questions"],
                            }
                        )
                    )
                    answers = result.answers
                    if len(answers) == len(quiz["questions"]):
                        return answers
                return answers

            failure_probes = M2FailureProbeRunner(
                db,
                knowledge_package_path=inputs.knowledge_package_path,
            )

            def rebuild_evidence() -> list[str]:
                db.expire_all()
                rebuilt = rebuild_user_projections(db, user_id=DEMO_USER_ID)
                return [
                    "projection-rebuild:"
                    + ",".join(f"{key}={value}" for key, value in sorted(rebuilt.items()))
                ]

            driver = M2HttpJourneyDriver(
                client=client,
                db=db,
                provider=config["provider_protocol"],
                model=config["provider_model"],
                answerer=answerer,
                failure_probe_runner=failure_probes.run,
                projection_rebuild_runner=rebuild_evidence,
                plan_input={
                    "shelfId": "shelf_technology",
                    "topic": "北京大学程序设计实习：递归、图搜索与动态规划",
                    "role": "计算机专业学习者",
                    "experience": "完成计算概论，具备基础 C++ 语法经验",
                    "purpose": "沿北大课程基准掌握递归、图搜索与动态规划的机制和应用",
                    "depth": "deep",
                    "details": (
                        "完整规划必须在全部章节覆盖课程基准的 8 个必需目标。"
                        "第一本书最前面的若干章必须先依次且合计只绑定 "
                        "solve_with_enumeration_recursion_and_search 与 "
                        "model_and_solve_with_dynamic_programming；不要强行把两者塞进同一章，"
                        "其余 6 个目标放在这组前置章之后。该前置章组的小节必须完成 "
                        "recursion、graph_search、dynamic_programming 三个概念，并逐节显式声明 "
                        "baselineConceptKey 与 baselineObjectiveKey。前置章组的标题、目标和内容"
                        "只讨论递归基本情形与推进、"
                        "DFS/BFS 图遍历以及动态规划的记忆化/制表机制；枚举、排序、C++ 语言特性"
                        "等虽可能共享课程目标但未进入本次发布知识图的主题必须放到后续章节。"
                        "规划时必须在每个前置章的 baseline_concept_ids 中只填写该章实际教授的"
                        "上述概念 key；递归章只填 recursion，图搜索章只填 graph_search，"
                        "动态规划章只填 dynamic_programming，合并章则填写它实际覆盖的组合。"
                        "不要把未进入已发布知识图的目标或子主题混入这组前置章。"
                    ),
                },
            )
            runner = M2AcceptanceRunner(
                db,
                driver=driver,
                auditor=SqlAlchemyM2EvidenceAuditor(db),
            )
            result = runner.run(inputs)
            result.write(
                json_path=output_dir / f"{run_id}.json",
                markdown_path=output_dir / f"{run_id}.md",
            )
            print(result.acceptance.decision)
            print(output_dir / f"{run_id}.json")
            print(output_dir / f"{run_id}.md")
            return 0 if result.acceptance.decision == "PASS" else 1
    finally:
        loop.run_until_complete(learner_adapter.close())
        loop.close()


if __name__ == "__main__":
    raise SystemExit(main())
