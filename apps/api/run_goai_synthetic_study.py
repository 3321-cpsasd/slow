"""Run the preregistered GOAI synthetic-learner paired study."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.ai.local_adapter import LocalDemoAdapter
from app.evaluation.runner import configured_provider, configured_provider_adapter
from app.evaluation.synthetic_study import (
    StudyCondition,
    prepare_episode_database,
    run_core_episode,
    stable_hash,
)
from app.main import build_provider_adapter
from app.services.source_verifier import AcceptingSourceVerifier


WORKSPACE = Path(__file__).resolve().parents[2]
DEFAULT_PERSONAS = (
    WORKSPACE / "output" / "goai-2026" / "agent-study" / "personas-v1.json"
)


def _markdown(report: dict) -> str:
    lines = [
        f"# Slow 合成学习者配对实验：{report['runId']}",
        "",
        f"- 执行模式：`{report['executionMode']}`",
        f"- 总结论：**{report['verdict']}**",
        f"- 核心旅程：{report['summary']['passedEpisodes']} / {report['summary']['episodeCount']} PASS",
        f"- 配对输出差异：{report['summary']['counterfactualDifferentPairs']} / {report['summary']['applicablePairs']} applicable pairs",
        "- 证据边界：该实验验证 Agent 与系统行为，不代表真人学习效果、满意度或留存。",
        "",
        "## 核心旅程",
        "",
        "| Persona | Repeat | Condition | Result | Duration | Input / Output tokens |",
        "| --- | ---: | --- | --- | ---: | ---: |",
    ]
    for episode in report["episodes"]:
        lines.append(
            f"| {episode['personaId']} | {episode['repeat']} | "
            f"{episode['condition']} | {episode['verdict']} | "
            f"{episode['durationSeconds']}s | "
            f"{episode['usage']['inputTokens']} / "
            f"{episode['usage']['outputTokens']} |"
        )
    lines.extend(["", "## 配对对照", ""])
    lines.extend(
        [
            "| Persona | Repeat | Applicable | Output differs | FULL hash | NO_MEMORY hash |",
            "| --- | ---: | --- | --- | --- | --- |",
        ]
    )
    for pair in report["pairs"]:
        lines.append(
            f"| {pair['personaId']} | {pair['repeat']} | "
            f"{pair['applicable']} | {pair['outputDiffers']} | "
            f"`{pair['fullOutputHash'][:12]}` | "
            f"`{pair['noMemoryOutputHash'][:12]}` |"
        )
    lines.extend(["", "## 硬门禁汇总", ""])
    for gate, value in sorted(report["summary"]["hardGateCounts"].items()):
        lines.append(
            f"- {gate}: `{value['passed']}/{value['total']}`"
        )
    lines.extend(
        [
            "",
            "## 声明边界",
            "",
            "本报告中的学习者均为版本化合成画像。首次失败是受控实验操作，"
            "并以 `forcedFailure=true` 保存；不得称为自然发生的真人错误。"
            "模型独立作答不等于真人学会，也不提供满意度、付费或留存证据。",
            "",
        ]
    )
    return "\n".join(lines)


def _pair_results(episodes: list[dict], personas: dict[str, dict]) -> list[dict]:
    def semantic_payload(episode: dict) -> list[dict]:
        return [
            {
                "kind": block.get("kind"),
                "role": block.get("role"),
                "heading": block.get("heading"),
                "content": block.get("content"),
                "assessmentObjectives": block.get("assessmentObjectives", []),
            }
            for block in episode.get("remediation", {}).get("blocks", [])
        ]

    grouped: dict[tuple[str, int], dict[str, dict]] = defaultdict(dict)
    for episode in episodes:
        grouped[(episode["personaId"], episode["repeat"])][
            episode["condition"]
        ] = episode
    pairs = []
    for (persona_id, repeat), conditions in sorted(grouped.items()):
        full = conditions.get("FULL")
        no_memory = conditions.get("NO_MEMORY")
        if not full or not no_memory:
            continue
        full_hash = stable_hash(semantic_payload(full))
        no_memory_hash = stable_hash(semantic_payload(no_memory))
        pairs.append(
            {
                "personaId": persona_id,
                "repeat": repeat,
                "applicable": bool(personas[persona_id]["memoryContrastApplicable"]),
                "outputDiffers": full_hash != no_memory_hash,
                "fullOutputHash": full_hash,
                "noMemoryOutputHash": no_memory_hash,
                "fullEpisodeId": full["episodeId"],
                "noMemoryEpisodeId": no_memory["episodeId"],
            }
        )
    return pairs


def _summary(episodes: list[dict], pairs: list[dict]) -> dict:
    gate_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {"passed": 0, "total": 0}
    )
    for episode in episodes:
        for gate, passed in episode.get("hardGates", {}).items():
            gate_counts[gate]["total"] += 1
            gate_counts[gate]["passed"] += int(bool(passed))
    applicable = [item for item in pairs if item["applicable"]]
    return {
        "episodeCount": len(episodes),
        "passedEpisodes": sum(item["verdict"] == "PASS" for item in episodes),
        "applicablePairs": len(applicable),
        "counterfactualDifferentPairs": sum(
            item["outputDiffers"] for item in applicable
        ),
        "hardGateCounts": dict(gate_counts),
        "inputTokens": sum(item["usage"]["inputTokens"] for item in episodes),
        "outputTokens": sum(item["usage"]["outputTokens"] for item in episodes),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["fixture", "real"],
        default="fixture",
        help="fixture validates orchestration only; real calls the configured provider",
    )
    parser.add_argument("--personas", type=Path, default=DEFAULT_PERSONAS)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=WORKSPACE / "reports" / "evaluations" / "goai-synthetic-study",
    )
    parser.add_argument("--repeat-count", type=int, default=2)
    parser.add_argument("--persona-id", action="append", default=[])
    parser.add_argument(
        "--condition",
        action="append",
        choices=["FULL", "NO_MEMORY"],
        default=[],
    )
    parser.add_argument("--keep-episode-databases", action="store_true")
    args = parser.parse_args()

    persona_document = json.loads(args.personas.read_text(encoding="utf-8"))
    if persona_document.get("status") != "frozen_v1":
        raise SystemExit("persona bindings must be frozen before execution")
    selected = [
        item
        for item in persona_document["personas"]
        if not args.persona_id or item["personaId"] in set(args.persona_id)
    ]
    if args.persona_id and len(selected) != len(set(args.persona_id)):
        raise SystemExit("one or more requested persona IDs do not exist")
    conditions: list[StudyCondition] = args.condition or ["FULL", "NO_MEMORY"]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime(
        "goai-synthetic-study-%Y%m%dT%H%M%SZ"
    )
    run_dir = args.output_dir / run_id
    episode_dir = run_dir / "episodes"
    database_dir = run_dir / "episode-databases"
    episode_dir.mkdir(parents=True, exist_ok=True)
    database_dir.mkdir(parents=True, exist_ok=True)

    provider_config = capabilities = None
    if args.mode == "real":
        provider_config, capabilities = configured_provider()
    episodes: list[dict] = []
    persona_by_id = {item["personaId"]: item for item in selected}
    for repeat in range(1, args.repeat_count + 1):
        for persona in selected:
            for condition in conditions:
                episode_id = (
                    f"{persona['personaId'].lower()}-r{repeat}-"
                    f"{condition.lower()}"
                )
                database_path = database_dir / f"{episode_id}.db"
                prepared = prepare_episode_database(
                    workspace=WORKSPACE,
                    persona=persona,
                    destination=database_path,
                )
                learner_loop = asyncio.new_event_loop()
                if args.mode == "real":
                    app_delegate = configured_provider_adapter()
                    learner_adapter = build_provider_adapter(
                        provider_config["api_key"],
                        provider_config["provider_model"],
                        provider_config["base_url"],
                        capabilities,
                    )

                    def learner_answerer(section, quiz, adapter=learner_adapter):
                        result = learner_loop.run_until_complete(
                            adapter.evaluation_quiz_answers(
                                {
                                    "section": section,
                                    "questions": quiz["questions"],
                                }
                            )
                        )
                        return result.answers

                    source_verifier = None
                    execution_mode = "real_provider"
                else:
                    app_delegate = LocalDemoAdapter()
                    learner_adapter = None

                    def learner_answerer(_section, quiz):
                        return [[1] for _ in quiz["questions"]]

                    source_verifier = AcceptingSourceVerifier()
                    execution_mode = "fixture"
                try:
                    episode = run_core_episode(
                        prepared=prepared,
                        persona=persona,
                        condition=condition,
                        app_delegate=app_delegate,
                        learner_answerer=learner_answerer,
                        execution_mode=execution_mode,
                        source_verifier=source_verifier,
                    )
                finally:
                    learner_loop.run_until_complete(app_delegate.close())
                    if learner_adapter:
                        learner_loop.run_until_complete(learner_adapter.close())
                    learner_loop.close()
                episode["episodeId"] = episode_id
                episode["repeat"] = repeat
                episode_path = episode_dir / f"{episode_id}.json"
                episode_path.write_text(
                    json.dumps(episode, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                episodes.append(episode)
                print(
                    json.dumps(
                        {
                            "episodeId": episode_id,
                            "verdict": episode["verdict"],
                            "durationSeconds": episode["durationSeconds"],
                            "error": episode["error"],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                if not args.keep_episode_databases:
                    database_path.unlink(missing_ok=True)

    pairs = _pair_results(episodes, persona_by_id)
    summary = _summary(episodes, pairs)
    full_shape = (
        len(selected) == 6
        and args.repeat_count == 2
        and set(conditions) == {"FULL", "NO_MEMORY"}
    )
    core_pass = bool(
        full_shape
        and summary["passedEpisodes"] == 24
        and summary["applicablePairs"] == 8
        and summary["counterfactualDifferentPairs"] >= 6
    )
    verdict = (
        "PASS"
        if args.mode == "real" and core_pass
        else "FIXTURE_ONLY"
        if args.mode == "fixture" and summary["passedEpisodes"] == len(episodes)
        else "INCOMPLETE"
        if not full_shape
        else "FAIL"
    )
    report = {
        "schemaVersion": "slow_synthetic_study_v1",
        "runId": run_id,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "executionMode": "real_provider" if args.mode == "real" else "fixture",
        "personaSchemaVersion": persona_document["schemaVersion"],
        "personaDocumentHash": stable_hash(persona_document),
        "doNotInterpretAsHumanOutcome": True,
        "episodes": episodes,
        "pairs": pairs,
        "summary": summary,
        "faultProbes": [],
        "verdict": verdict,
    }
    json_path = run_dir / f"{run_id}.json"
    markdown_path = run_dir / f"{run_id}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown_path.write_text(_markdown(report), encoding="utf-8")
    if not args.keep_episode_databases:
        shutil.rmtree(database_dir, ignore_errors=True)
    print(
        json.dumps(
            {
                "verdict": verdict,
                "json": str(json_path),
                "markdown": str(markdown_path),
            },
            ensure_ascii=False,
        )
    )
    return 0 if verdict in {"PASS", "FIXTURE_ONLY", "INCOMPLETE"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
