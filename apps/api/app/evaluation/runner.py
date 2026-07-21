import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from ..ai.local_adapter import LocalDemoAdapter
from ..ai.openai_adapter import OpenAiAdapter
from ..core.config import ROOT, settings
from ..main import create_app
from ..infrastructure.database import build_database
from ..infrastructure.tables import Base, EvaluationRun, now
from ..services.source_verifier import AcceptingSourceVerifier
from ..services.attachment_storage import LocalAttachmentStorage


@dataclass
class Step:
    name: str
    status: str
    evidence: dict

    def as_dict(self):
        return {"name": self.name, "status": self.status, "evidence": self.evidence}


class LearnerRunner:
    """Black-box learner: all product interaction goes through public HTTP routes."""

    def __init__(self, client: TestClient, answerer=None, existing_series_id=None):
        self.client = client
        self.answerer = answerer or (lambda _section, quiz: [[1] for _ in quiz["questions"]])
        self.existing_series_id = existing_series_id
        self.steps: list[Step] = []

    def request(self, method, path, *, json_body=None, content=None, headers=None, expected=200):
        response = self.client.request(method, path, json=json_body, content=content, headers=headers)
        payload = response.json() if response.content else {}
        self.steps.append(Step(f"{method} {path}", "PASS" if response.status_code == expected else "FAIL", {"statusCode": response.status_code, "payload": payload}))
        if response.status_code != expected:
            raise RuntimeError(f"{method} {path}: expected {expected}, got {response.status_code}: {payload}")
        return payload

    def run(self):
        health = self.request("GET", "/api/health")
        if self.existing_series_id:
            series = self.request("GET", f"/api/series/{self.existing_series_id}")
        else:
            series = self.request(
                "POST",
                "/api/plans",
                json_body={
                    "shelfId": "shelf_technology",
                    "topic": "Kubernetes",
                    "role": "技术人员",
                    "experience": "熟悉 Linux、容器与基础网络",
                    "purpose": "完成部署和日常排障",
                    "depth": "deep",
                    "details": "理解机制、边界与迁移",
                },
                expected=201,
            )
        first_book = series["books"][0]
        locked_chapter = next((book["chapters"][0]["id"] for book in series["books"] if book["status"] == "locked" and book.get("chapters")), None)
        if locked_chapter:
            self.request("POST", f"/api/chapters/{locked_chapter}/generate", expected=403)
        editable = first_book["chapters"][-1]
        if "（已校准）" in editable["title"]:
            edited = editable
        else:
            edited = self.request("PATCH", f"/api/chapters/{editable['id']}", json_body={"title": f"{editable['title']}（已校准）"})
        added = self.request("POST", f"/api/books/{first_book['id']}/chapters", json_body={"title": "临时未来章节", "objective": "验证未来章节编辑"}, expected=201)
        self.request("DELETE", f"/api/chapters/{added['id']}", expected=204)
        first_section_id = None
        stable_blocks = True
        sources_verified = True
        remediation_verified = False
        qa_correction_verified = False
        for chapter_summary in first_book["chapters"]:
            chapter = self.request("POST", f"/api/chapters/{chapter_summary['id']}/generate")
            for section_summary in chapter["sections"]:
                first_section_id = first_section_id or section_summary["id"]
                if section_summary["status"] == "completed":
                    completed = self.request("GET", f"/api/sections/{section_summary['id']}")
                    stable_blocks = stable_blocks and all(block.get("id") for block in completed["content"]["blocks"])
                    sources_verified = sources_verified and all(item.get("reachable") and item.get("pinned") for item in completed["content"]["sourceVerification"])
                    remediation_verified = remediation_verified or bool(completed.get("remediations"))
                    if section_summary["id"] == first_section_id and not qa_correction_verified:
                        block_id = completed["content"]["blocks"][0]["id"]
                        first_qa = self.request("POST", f"/api/sections/{section_summary['id']}/ask", json_body={"blockId": block_id, "question": "请再次确认这个机制的边界。", "forceRelation": "new_question"})
                        second_qa = self.request("POST", f"/api/sections/{section_summary['id']}/ask", json_body={"blockId": block_id, "question": "这是上一问题的追问。", "forceRelation": "new_question"})
                        corrected = self.request("PATCH", f"/api/sections/{section_summary['id']}/qa/threads/{second_qa['threadId']}", json_body={"relation": "follow_up", "targetThreadId": first_qa["threadId"]})
                        qa_correction_verified = corrected["corrected"]
                    continue
                section = self.request("POST", f"/api/sections/{section_summary['id']}/generate")
                stable_blocks = stable_blocks and all(block.get("id") for block in section["content"]["blocks"])
                sources_verified = sources_verified and all(item.get("reachable") and item.get("pinned") for item in section["content"]["sourceVerification"])
                block_id = section["content"]["blocks"][0]["id"]
                first_qa = self.request("POST", f"/api/sections/{section['id']}/ask", json_body={"blockId": block_id, "question": "这个机制的边界是什么？"})
                if section["id"] == first_section_id and not section.get("remediations"):
                    second_qa = self.request("POST", f"/api/sections/{section['id']}/ask", json_body={"blockId": block_id, "question": "换一个新问题", "forceRelation": "new_question"})
                    corrected = self.request("PATCH", f"/api/sections/{section['id']}/qa/threads/{second_qa['threadId']}", json_body={"relation": "follow_up", "targetThreadId": first_qa["threadId"]})
                    qa_correction_verified = corrected["corrected"]
                quiz = section["quiz"]
                if section["id"] == first_section_id:
                    failed_answers = [[] for _ in quiz["questions"]]
                    failed = self.request("POST", f"/api/sections/{section['id']}/quiz", json_body={"quizSetId": quiz["id"], "answers": failed_answers})
                    remediation_verified = bool(failed.get("remediation", {}).get("blocks")) and failed["nextQuiz"]["id"] != quiz["id"]
                    quiz = failed["nextQuiz"]
                answers = self.answerer(section, quiz)
                if len(answers) != len(quiz["questions"]):
                    raise RuntimeError("independent learner returned the wrong answer count")
                result = self.request("POST", f"/api/sections/{section['id']}/quiz", json_body={"quizSetId": quiz["id"], "answers": answers})
                if not result["passed"]:
                    raise RuntimeError("learner answer strategy did not pass current quiz")
                completed = self.request("GET", f"/api/sections/{section['id']}")
                if not completed["note"]:
                    raise RuntimeError("passing quiz did not create note")
            attachment = self.request(
                "POST",
                f"/api/chapters/{chapter['id']}/practice/attachments",
                content=b"evaluation practice artifact\n",
                headers={"x-filename": "practice.txt", "content-type": "text/plain"},
                expected=201,
            )
            self.request("POST", f"/api/chapters/{chapter['id']}/practice", json_body={"content": {"evidence": "evaluation artifact", "reflection": "verified"}, "attachmentIds": [attachment["id"]]})
        capstone_attachment = self.request(
            "POST",
            f"/api/books/{first_book['id']}/capstone/attachments",
            content=b"evaluation capstone artifact\n",
            headers={"x-filename": "capstone.txt", "content-type": "text/plain"},
            expected=201,
        )
        self.request("POST", f"/api/books/{first_book['id']}/capstone", json_body={"content": {"artifact": "evaluation capstone", "verification": "passed"}, "attachmentIds": [capstone_attachment["id"]]})
        if first_section_id:
            started = self.client.post(f"/api/sections/{first_section_id}/ask-me", json={"answer": ""})
            if started.status_code not in (200, 201) and started.json().get("code") != "ASK_ME_ANSWER_REQUIRED":
                raise RuntimeError(f"POST /api/sections/{first_section_id}/ask-me: expected a new or resumable session, got {started.status_code}: {started.json()}")
            for answer in ["机制回答", "边界回答", "迁移回答"]:
                self.request("POST", f"/api/sections/{first_section_id}/ask-me", json_body={"answer": answer})
        final_series = self.request("GET", f"/api/series/{series['id']}")
        memory = self.request("GET", "/api/learning-memory?shelf_id=shelf_technology")
        second_book_entered = len(final_series["books"]) == 1 or final_series["books"][1]["status"] == "available"
        cross_book_adaptation_trace = False
        second_book_section_id = None
        if len(final_series["books"]) > 1 and final_series["books"][1]["status"] in {"available", "in_progress"}:
            second_chapter = self.request("POST", f"/api/chapters/{final_series['books'][1]['chapters'][0]['id']}/generate")
            second_book_section_id = second_chapter["sections"][0]["id"]
            second_section = self.request("POST", f"/api/sections/{second_book_section_id}/generate")
            cross_book_adaptation_trace = bool(second_section.get("generation", {}).get("trace", {}).get("memoryApplied"))
        return {
            "interface": "public HTTP API only",
            "health": health,
            "seriesId": series["id"],
            "firstBookCompleted": final_series["books"][0]["status"] == "completed",
            "secondBookEntered": second_book_entered,
            "progress": final_series["progress"],
            "featureEvidence": {
                "stableBlocks": stable_blocks,
                "sourceVerification": sources_verified,
                "remediation": remediation_verified,
                "qaThreads": qa_correction_verified,
                "futureChapterEditing": edited["title"].endswith("（已校准）"),
                "learningMemory": bool(memory),
                "crossBookAdaptationTrace": cross_book_adaptation_trace,
                "secondBookGeneratedSectionId": second_book_section_id,
                "artifactAttachments": any("/attachments" in item.name for item in self.steps),
            },
            "steps": [item.as_dict() for item in self.steps],
        }


class GateReviewer:
    """Independent deterministic reviewer: consumes raw evidence, never learner verdict text."""

    GATES = {
        "G01": "Frontend/backend separation",
        "G02": "Hierarchy and dynamic sections",
        "G03": "Stable content block identity and sources",
        "G04": "Server-side unlock and grading",
        "G05": "Persistent remediation and novel retry",
        "G06": "Weighted and correctable Q&A threads",
        "G07": "Durable notes with separate user content",
        "G08": "Three-round Ask Me",
        "G09": "Chapter practice and book capstone",
        "G10": "Future chapter adjustment",
        "G11": "Server-side source verification",
        "G12": "Cross-book evidence and memory",
        "G13": "Future-aware series progress",
        "G14": "Reusable dual-format evaluation runner",
        "G15": "First book completion into second book",
        "G16": "Frontend build",
        "G17": "Backend tests",
    }

    def review(self, learner, deterministic):
        steps_ok = all(item["status"] == "PASS" for item in learner["steps"])
        runtime_checks = {
            "G01": deterministic.get("frontendBuild", False),
            "G02": steps_ok,
            "G03": deterministic.get("stableBlocks", False),
            "G04": steps_ok,
            "G05": deterministic.get("remediation", False),
            "G06": deterministic.get("qaThreads", False),
            "G07": steps_ok,
            "G08": any("/ask-me" in item["name"] for item in learner["steps"]) and steps_ok,
            "G09": deterministic.get("artifactAttachments", False) and any("/practice" in item["name"] for item in learner["steps"]) and any("/capstone" in item["name"] for item in learner["steps"]),
            "G10": deterministic.get("futureChapterEditing", False),
            "G11": deterministic.get("sourceVerification", False),
            "G12": deterministic.get("learningMemory", False) and deterministic.get("crossBookAdaptationTrace", False),
            "G13": learner["progress"] < 100 or learner["secondBookEntered"],
            "G14": deterministic.get("runnerPersistence", False),
            "G15": deterministic.get("realAi", False) and learner["firstBookCompleted"] and learner["secondBookEntered"],
            "G16": deterministic.get("frontendBuild", False),
            "G17": deterministic.get("backendTests", False),
        }
        gates = [{"id": gate_id, "name": name, "status": "PASS" if runtime_checks[gate_id] else "FAIL"} for gate_id, name in self.GATES.items()]
        return {"policy": "evidence-insufficient-means-fail", "gates": gates, "passed": sum(item["status"] == "PASS" for item in gates), "failed": sum(item["status"] == "FAIL" for item in gates), "verdict": "PASS" if all(item["status"] == "PASS" for item in gates) else "FAIL"}


def markdown_report(report):
    lines = [
        f"# Slow evaluation {report['runId']}",
        "",
        f"- Mode: `{report['mode']}`",
        f"- Model: `{report['model']}`",
        f"- Verdict: **{report['review']['verdict']}**",
        f"- Gates: {report['review']['passed']} passed / {report['review']['failed']} failed",
        "",
        "## Hard gates",
        "",
        "| Gate | Result | Name |",
        "|---|---|---|",
    ]
    lines.extend(f"| {item['id']} | {item['status']} | {item['name']} |" for item in report["review"]["gates"])
    lines.extend(["", "## Journey", "", f"- First book completed: `{report['learner']['firstBookCompleted']}`", f"- Second book entered: `{report['learner']['secondBookEntered']}`", f"- Saved HTTP steps: `{len(report['learner']['steps'])}`", ""])
    if report.get("runnerError"):
        lines.extend(["## Runner error", "", f"- Type: `{report['runnerError']['type']}`", f"- Stage: `{report['runnerError']['stage']}`", f"- Message: `{report['runnerError']['message']}`", ""])
    if report.get("evidenceSnapshot"):
        lines.extend(["## Evidence snapshot", "", f"- Database: `{report['evidenceSnapshot']['database']['path']}`", f"- SHA-256: `{report['evidenceSnapshot']['database']['sha256']}`", f"- Attachment objects: `{report['evidenceSnapshot']['attachments']['count']}`", ""])
    return "\n".join(lines)


def run_real_smoke(output_dir: Path):
    run_id = datetime.now(timezone.utc).strftime("slow-real-smoke-%Y%m%dT%H%M%SZ")
    database_path = Path(tempfile.gettempdir()) / f"{run_id}.db"
    started = time.monotonic()
    app = create_app(f"sqlite+pysqlite:///{database_path}")
    steps = []
    health, persisted, adapter, error = {"model": settings.openai_model}, {}, None, None
    try:
        with TestClient(app) as client:
            adapter = client.app.state.ai

            def call(method, path, body=None, expected=200):
                response = client.request(method, path, json=body)
                payload = response.json() if response.content else {}
                steps.append({"method": method, "path": path, "statusCode": response.status_code, "payload": payload})
                if response.status_code != expected:
                    if method == "POST" and path.startswith("/api/sections/") and path.endswith("/generate"):
                        section_id = path.split("/")[3]
                        state = client.get(f"/api/sections/{section_id}")
                        steps.append({"method": "GET", "path": f"/api/sections/{section_id}", "statusCode": state.status_code, "payload": state.json()})
                    raise RuntimeError(f"{method} {path} failed: {response.status_code} {payload}")
                return payload

            health = call("GET", "/api/health")
            series = call("POST", "/api/plans", {"shelfId": "shelf_technology", "topic": "Kubernetes", "role": "技术人员", "experience": "熟悉 Linux、Docker 和基础网络，但没有 Kubernetes 实践经验", "purpose": "参与 Kubernetes 应用部署与日常排障", "depth": "deep", "details": "理解核心机制而不是记命令"}, 201)
            chapter = call("POST", f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate")
            section = call("POST", f"/api/sections/{chapter['sections'][0]['id']}/generate")
            persisted = call("GET", f"/api/sections/{section['id']}")
    except Exception as exc:
        error = str(exc)
        if steps and steps[-1]["method"] == "GET" and steps[-1]["path"].startswith("/api/sections/"):
            persisted = steps[-1]["payload"]
    passed = bool(not error and persisted.get("content") and persisted.get("quiz") and persisted.get("generation", {}).get("status") == "succeeded")
    content = persisted.get("content") or {}
    report = {
        "schemaVersion": "1.0",
        "runId": run_id,
        "mode": "real-smoke",
        "model": health["model"],
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "durationSeconds": round(time.monotonic() - started, 3),
        "usage": {"inputTokens": getattr(adapter, "input_tokens", 0), "outputTokens": getattr(adapter, "output_tokens", 0)},
        "verdict": "PASS" if passed else "FAIL",
        "error": error,
        "assertions": {"contentPersisted": bool(content), "quizPersisted": bool(persisted.get("quiz")), "stableBlockIds": bool(content) and all(item.get("id") for item in content.get("blocks", [])), "sourcesReachable": bool(content) and all(item.get("reachable") for item in content.get("sourceVerification", []))},
        "steps": steps,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path, markdown_path = output_dir / f"{run_id}.json", output_dir / f"{run_id}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(f"# Slow real-AI smoke {run_id}\n\n- Model: `{report['model']}`\n- Verdict: **{report['verdict']}**\n- Duration: {report['durationSeconds']}s\n- Content persisted: `{report['assertions']['contentPersisted']}`\n- Quiz persisted: `{report['assertions']['quizPersisted']}`\n- Stable block IDs: `{report['assertions']['stableBlockIds']}`\n- Sources reachable: `{report['assertions']['sourcesReachable']}`\n", encoding="utf-8")
    return json_path, markdown_path, report


def resume_real_section_smoke(output_dir: Path, database_path: Path, section_id: str):
    run_id = datetime.now(timezone.utc).strftime("slow-real-section-retry-%Y%m%dT%H%M%SZ")
    started, steps, error = time.monotonic(), [], None
    app = create_app(f"sqlite+pysqlite:///{database_path}")
    persisted, adapter = {}, None
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            adapter = client.app.state.ai
            generated = client.post(f"/api/sections/{section_id}/generate")
            steps.append({"method": "POST", "path": f"/api/sections/{section_id}/generate", "statusCode": generated.status_code, "payload": generated.json()})
            state = client.get(f"/api/sections/{section_id}")
            persisted = state.json()
            steps.append({"method": "GET", "path": f"/api/sections/{section_id}", "statusCode": state.status_code, "payload": persisted})
            if generated.status_code != 200:
                error = generated.json().get("error", "section generation failed")
    except Exception as exc:
        error = str(exc)
    content = persisted.get("content") or {}
    passed = bool(not error and content and persisted.get("quiz") and persisted.get("generation", {}).get("status") == "succeeded")
    report = {"schemaVersion": "1.0", "runId": run_id, "mode": "real-section-retry", "model": settings.openai_model, "createdAt": datetime.now(timezone.utc).isoformat(), "durationSeconds": round(time.monotonic() - started, 3), "usage": {"inputTokens": getattr(adapter, "input_tokens", 0), "outputTokens": getattr(adapter, "output_tokens", 0)}, "verdict": "PASS" if passed else "FAIL", "error": error, "assertions": {"contentPersisted": bool(content), "quizPersisted": bool(persisted.get("quiz")), "generationStatus": persisted.get("generation", {}).get("status"), "stableBlockIds": bool(content) and all(item.get("id") for item in content.get("blocks", [])), "sourcesReachable": bool(content) and all(item.get("reachable") for item in content.get("sourceVerification", []))}, "steps": steps}
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path, markdown_path = output_dir / f"{run_id}.json", output_dir / f"{run_id}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(f"# Slow real section retry {run_id}\n\n- Verdict: **{report['verdict']}**\n- Duration: {report['durationSeconds']}s\n- Generation: `{report['assertions']['generationStatus']}`\n- Error: `{error or ''}`\n", encoding="utf-8")
    return json_path, markdown_path, report


def _code_version():
    digest = hashlib.sha256()
    for path in sorted((ROOT / "apps").rglob("*")):
        if path.is_file() and path.suffix in {".py", ".ts", ".tsx"} and "node_modules" not in path.parts and "dist" not in path.parts:
            digest.update(path.relative_to(ROOT).as_posix().encode())
            digest.update(path.read_bytes())
    return f"sha256:{digest.hexdigest()[:16]}"


def _file_sha256(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _persist_evaluation(database_path: Path, report, json_path: Path, markdown_path: Path):
    engine, sessions = build_database(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(engine)
    with sessions() as history:
        record = history.get(EvaluationRun, report["runId"])
        payload = json.dumps({"jsonReport": str(json_path), "markdownReport": str(markdown_path), "runnerError": report.get("runnerError"), "review": report["review"], "semanticReview": report["semanticReview"], "evidenceSnapshot": report.get("evidenceSnapshot")}, ensure_ascii=False)
        if record:
            record.status, record.result_json, record.finished_at = report["review"]["verdict"].lower(), payload, now()
        else:
            history.add(EvaluationRun(id=report["runId"], mode=report["mode"], model=report["model"], status=report["review"]["verdict"].lower(), code_version=report["codeVersion"], prompt_version=report["promptVersion"], result_json=payload, started_at=datetime.fromisoformat(report["createdAt"]), finished_at=now()))
        history.commit()
    engine.dispose()


def run(output_dir: Path, real=False, deterministic=None, database_path_override=None, existing_series_id=None):
    started = time.monotonic()
    started_at = datetime.now(timezone.utc)
    run_id = started_at.strftime("slow-eval-%Y%m%dT%H%M%SZ")
    database_path = Path(database_path_override) if database_path_override else Path(tempfile.gettempdir()) / f"{run_id}.db"
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path, markdown_path = output_dir / f"{run_id}.json", output_dir / f"{run_id}.md"
    evaluation_attachment_dir = output_dir / "evidence" / f"{run_id}-attachments"
    evaluation_storage = LocalAttachmentStorage(evaluation_attachment_dir, settings.attachment_max_bytes)
    agent_loop, learner_agent, reviewer_agent, adapter = None, None, None, None
    learner_runner = None
    runner_error = None
    if real:
        app = create_app(f"sqlite+pysqlite:///{database_path}", attachment_storage=evaluation_storage)
        model = settings.openai_model
        agent_loop = asyncio.new_event_loop()
        learner_agent = OpenAiAdapter(settings.openai_api_key, settings.openai_model, settings.openai_base_url)

        def answerer(section, quiz):
            result = agent_loop.run_until_complete(learner_agent.evaluation_quiz_answers({"section": {"id": section["id"], "title": section["title"], "question": section["question"], "content": section["content"]}, "questions": quiz["questions"]}))
            return result.answers
    else:
        app = create_app(f"sqlite+pysqlite:///{database_path}", LocalDemoAdapter(), AcceptingSourceVerifier(), evaluation_storage)
        model = LocalDemoAdapter.model
        answerer = None
    try:
        with TestClient(app) as client:
            adapter = client.app.state.ai
            learner_runner = LearnerRunner(client, answerer, existing_series_id)
            learner = learner_runner.run()
    except Exception as exc:
        runner_error = {"type": type(exc).__name__, "message": str(exc)[:2000], "stage": "learner_journey"}
        steps = [item.as_dict() for item in learner_runner.steps] if learner_runner else []
        last_series = next((item["evidence"]["payload"] for item in reversed(steps) if item["name"].startswith("GET /api/series/") and item["status"] == "PASS"), {})
        books = last_series.get("books", []) if isinstance(last_series, dict) else []
        steps.append({"name": "RUN learner journey", "status": "FAIL", "evidence": {"error": runner_error}})
        learner = {"interface": "public HTTP API only", "health": {}, "seriesId": existing_series_id, "firstBookCompleted": bool(books and books[0]["status"] == "completed"), "secondBookEntered": bool(len(books) > 1 and books[1]["status"] in {"available", "in_progress", "completed"}), "progress": last_series.get("progress", 0) if isinstance(last_series, dict) else 0, "featureEvidence": {}, "steps": steps}

    checks = deterministic or {"frontendBuild": False, "backendTests": False}
    deterministic_result = {**learner["featureEvidence"], "realAi": real, "runnerPersistence": False, **checks}
    review = GateReviewer().review(learner, deterministic_result)
    semantic_review = {"status": "NOT_RUN", "reason": "local runs cannot provide an independent real-AI semantic verdict"}
    if real and not runner_error:
        section_samples_by_id = {}
        note_samples_by_id = {}
        for item in learner["steps"]:
            payload = item["evidence"].get("payload")
            if not isinstance(payload, dict) or not payload.get("id"):
                continue
            if item["name"].startswith(("POST /api/sections/", "GET /api/sections/")) and payload.get("content"):
                section_samples_by_id[payload["id"]] = payload
            if item["name"].startswith("GET /api/sections/") and payload.get("note"):
                note_samples_by_id[payload["id"]] = payload
        second_id = learner["featureEvidence"].get("secondBookGeneratedSectionId")
        selected_ids = list(section_samples_by_id)[:5]
        if second_id and second_id not in selected_ids:
            selected_ids.append(second_id)
        samples = [section_samples_by_id[item_id] for item_id in selected_ids if item_id in section_samples_by_id]
        note_samples = list(note_samples_by_id.values())[:5]
        reviewer_agent = OpenAiAdapter(settings.openai_api_key, settings.openai_model, settings.openai_base_url)
        try:
            assessed = agent_loop.run_until_complete(reviewer_agent.review_evaluation({"fixedInput": {"topic": "Kubernetes", "role": "技术人员"}, "deterministicGates": review["gates"], "featureEvidence": learner["featureEvidence"], "contentSamples": samples, "noteSamples": note_samples, "journey": {"firstBookCompleted": learner["firstBookCompleted"], "secondBookEntered": learner["secondBookEntered"], "progress": learner["progress"]}}))
            semantic_review = {"status": "COMPLETED", **assessed.model_dump()}
        except Exception as exc:
            semantic_review = {"status": "FAILED", "error": str(exc)[:1000]}

    report = {
        "schemaVersion": "1.1",
        "runId": run_id,
        "createdAt": started_at.isoformat(),
        "durationSeconds": round(time.monotonic() - started, 3),
        "mode": "real" if real else "local",
        "model": model,
        "promptVersion": "slow-v0.3",
        "codeVersion": _code_version(),
        "databaseSource": str(database_path),
        "usage": {"inputTokens": sum(getattr(item, "input_tokens", 0) for item in [adapter, learner_agent, reviewer_agent] if item), "outputTokens": sum(getattr(item, "output_tokens", 0) for item in [adapter, learner_agent, reviewer_agent] if item)},
        "cost": {"currency": "USD", "amount": None, "reason": "provider price is not configured; token usage is preserved"},
        "fixedInput": {"topic": "Kubernetes", "role": "技术人员", "experience": "Linux, Docker, basic networking; no Kubernetes practice", "purpose": "deployment and troubleshooting", "depth": "deep"},
        "runnerError": runner_error,
        "learner": learner,
        "deterministic": deterministic_result,
        "review": review,
        "semanticReview": semantic_review,
        "evidenceSnapshot": None,
    }
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(markdown_report(report), encoding="utf-8")
    try:
        _persist_evaluation(database_path, report, json_path, markdown_path)
        evidence_dir = output_dir / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        snapshot = evidence_dir / f"{run_id}.db"
        shutil.copy2(database_path, snapshot)
        attachment_files = sorted(path for path in evaluation_attachment_dir.rglob("*") if path.is_file())
        report["evidenceSnapshot"] = {
            "database": {"path": str(snapshot), "sha256": _file_sha256(snapshot), "byteSize": snapshot.stat().st_size},
            "attachments": {"path": str(evaluation_attachment_dir), "count": len(attachment_files), "objects": [{"key": path.relative_to(evaluation_attachment_dir).as_posix(), "sha256": _file_sha256(path), "byteSize": path.stat().st_size} for path in attachment_files]},
        }
        deterministic_result["runnerPersistence"] = True
        report["review"] = GateReviewer().review(learner, deterministic_result)
        if runner_error or semantic_review.get("status") == "FAILED" or semantic_review.get("verdict") == "FAIL":
            report["review"]["verdict"] = "FAIL"
        _persist_evaluation(database_path, report, json_path, markdown_path)
    except Exception as exc:
        report["runnerPersistenceError"] = {"type": type(exc).__name__, "message": str(exc)[:2000]}
        report["review"]["verdict"] = "FAIL"
    finally:
        if agent_loop:
            if learner_agent:
                agent_loop.run_until_complete(learner_agent.close())
            if reviewer_agent:
                agent_loop.run_until_complete(reviewer_agent.close())
            agent_loop.close()
    report["durationSeconds"] = round(time.monotonic() - started, 3)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(markdown_report(report), encoding="utf-8")
    return json_path, markdown_path, report


def main():
    parser = argparse.ArgumentParser(description="Run the Slow black-box learner and independent gate reviewer")
    parser.add_argument("--real", action="store_true", help="use configured external model and real source reachability checks")
    parser.add_argument("--smoke", action="store_true", help="run only the real plan-to-first-section persistence smoke test")
    parser.add_argument("--resume-database", type=Path, help="reuse a smoke database and retry only one section")
    parser.add_argument("--resume-section", help="section ID to retry with --resume-database")
    parser.add_argument("--resume-series", help="series ID to continue through the full real learner journey")
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "evaluations")
    parser.add_argument("--skip-build-checks", action="store_true", help="do not run pytest and the frontend production build")
    args = parser.parse_args()
    if args.resume_section:
        if not args.real or not args.resume_database:
            parser.error("section retry requires --real, --resume-database and --resume-section")
        json_path, markdown_path, report = resume_real_section_smoke(args.output, args.resume_database, args.resume_section)
        print(json.dumps({"verdict": report["verdict"], "json": str(json_path), "markdown": str(markdown_path)}, ensure_ascii=False))
        return
    if args.resume_database and not args.resume_series:
        parser.error("full-run resume requires --resume-series")
    if args.resume_series and (not args.real or not args.resume_database):
        parser.error("full-run resume requires --real, --resume-database and --resume-series")
    if args.smoke:
        if not args.real:
            parser.error("--smoke requires --real")
        json_path, markdown_path, report = run_real_smoke(args.output)
        print(json.dumps({"verdict": report["verdict"], "json": str(json_path), "markdown": str(markdown_path)}, ensure_ascii=False))
        return
    checks = None
    if not args.skip_build_checks:
        backend = subprocess.run([str(ROOT / ".venv" / "bin" / "pytest"), "-q", "apps/api/tests"], cwd=ROOT, env={**os.environ, "PYTHONPATH": "apps/api"}, capture_output=True, text=True)
        pnpm = shutil.which("pnpm") or "pnpm"
        node = shutil.which("node")
        if not node:
            bundled_node_dir = Path(pnpm).resolve().parents[2] / "node" / "bin"
            node = str(bundled_node_dir) if bundled_node_dir.exists() else ""
        frontend_env = {**os.environ, "CI": "true", "PATH": os.pathsep.join(value for value in [node, os.environ.get("PATH", "")] if value)}
        frontend = subprocess.run([pnpm, "build"], cwd=ROOT / "apps" / "web", env=frontend_env, capture_output=True, text=True)
        checks = {"backendTests": backend.returncode == 0, "frontendBuild": frontend.returncode == 0}
    json_path, markdown_path, report = run(args.output, real=args.real, deterministic=checks, database_path_override=args.resume_database, existing_series_id=args.resume_series)
    print(json.dumps({"verdict": report["review"]["verdict"], "json": str(json_path), "markdown": str(markdown_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
