"""Import an evaluation-generated catalog into a real learner account.

Only curriculum/content facts are copied. Evaluation-agent learning facts such
as attempts, evidence, notes, remediation, mastery, and submissions are
deliberately excluded. The target learner receives a fresh learning run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def rows(db: sqlite3.Connection, query: str, params: tuple = ()) -> list[sqlite3.Row]:
    return list(db.execute(query, params))


def row(db: sqlite3.Connection, query: str, params: tuple = ()) -> sqlite3.Row | None:
    return db.execute(query, params).fetchone()


def snapshot_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_database(db: sqlite3.Connection, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(target) as backup:
        db.backup(backup)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--shelf-id", required=True)
    parser.add_argument("--backup-dir", type=Path, default=Path("data/backups"))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source_path = args.source.resolve()
    target_path = args.target.resolve()
    if source_path == target_path:
        raise SystemExit("source and target databases must be different")
    if not source_path.is_file() or not target_path.is_file():
        raise SystemExit("source and target databases must both exist")

    source_sha = snapshot_sha256(source_path)
    marker = f"evaluation_snapshot_sha256:{source_sha}"
    imported_at = timestamp()

    source = sqlite3.connect(source_path)
    target = sqlite3.connect(target_path)
    source.row_factory = sqlite3.Row
    target.row_factory = sqlite3.Row
    source.execute("PRAGMA foreign_keys=ON")
    target.execute("PRAGMA foreign_keys=ON")

    try:
        source_series = rows(
            source,
            "SELECT s.*, p.topic, p.role, p.experience, p.purpose, p.depth, "
            "p.details, p.assumptions_json, p.confidence "
            "FROM series s JOIN learning_plans p ON p.id=s.plan_id "
            "WHERE s.deleted_at IS NULL",
        )
        if len(source_series) != 1:
            raise SystemExit(f"expected exactly one active source series, found {len(source_series)}")
        source_series_row = source_series[0]

        target_shelf = row(
            target,
            "SELECT id FROM shelves WHERE id=? AND user_id=?",
            (args.shelf_id, args.user_id),
        )
        if not target_shelf:
            raise SystemExit("target shelf does not belong to target user")
        existing = row(
            target,
            "SELECT s.id, s.title FROM series s "
            "JOIN learning_plans p ON p.id=s.plan_id "
            "WHERE s.shelf_id=? AND instr(p.details, ?) > 0 AND s.deleted_at IS NULL",
            (args.shelf_id, marker),
        )
        if existing:
            print(json.dumps({"status": "already_imported", "seriesId": existing["id"], "title": existing["title"]}, ensure_ascii=False))
            return

        source_books = rows(source, "SELECT * FROM books WHERE series_id=? AND deleted_at IS NULL ORDER BY position", (source_series_row["id"],))
        source_chapters = rows(
            source,
            "SELECT c.* FROM chapters c JOIN books b ON b.id=c.book_id "
            "WHERE b.series_id=? AND b.deleted_at IS NULL ORDER BY b.position, c.position",
            (source_series_row["id"],),
        )
        source_sections = rows(
            source,
            "SELECT sec.* FROM sections sec JOIN chapters c ON c.id=sec.chapter_id "
            "JOIN books b ON b.id=c.book_id WHERE b.series_id=? AND b.deleted_at IS NULL "
            "ORDER BY b.position, c.position, sec.position",
            (source_series_row["id"],),
        )
        section_ids = [item["id"] for item in source_sections]
        placeholders = ",".join("?" for _ in section_ids) or "NULL"
        source_contents = rows(source, f"SELECT * FROM content_versions WHERE section_id IN ({placeholders}) ORDER BY section_id, version", tuple(section_ids))
        content_ids = [item["id"] for item in source_contents]
        content_placeholders = ",".join("?" for _ in content_ids) or "NULL"
        source_verifications = rows(source, f"SELECT * FROM source_verifications WHERE content_version_id IN ({content_placeholders})", tuple(content_ids))
        source_quizzes = rows(
            source,
            f"SELECT * FROM quiz_sets WHERE content_version_id IN ({content_placeholders}) AND generation=1",
            tuple(content_ids),
        )
        source_runs = rows(
            source,
            f"SELECT * FROM generation_runs WHERE section_id IN ({placeholders}) AND operation='lesson' AND status='succeeded'",
            tuple(section_ids),
        )
        source_capstones = rows(
            source,
            "SELECT bc.* FROM book_capstones bc JOIN books b ON b.id=bc.book_id WHERE b.series_id=?",
            (source_series_row["id"],),
        )
        source_practices = rows(
            source,
            "SELECT cp.* FROM chapter_practices cp JOIN chapters c ON c.id=cp.chapter_id "
            "JOIN books b ON b.id=c.book_id WHERE b.series_id=?",
            (source_series_row["id"],),
        )
        summary = {
            "title": source_series_row["title"],
            "books": len(source_books),
            "chapters": len(source_chapters),
            "sections": len(source_sections),
            "contentVersions": len(source_contents),
            "quizSets": len(source_quizzes),
            "sourceSha256": source_sha,
        }
        if args.dry_run:
            print(json.dumps({"status": "validated", **summary}, ensure_ascii=False))
            return

        backup_name = f"{target_path.stem}-before-evaluation-import-{datetime.now().strftime('%Y%m%dT%H%M%S')}.db"
        backup_path = args.backup_dir.resolve() / backup_name
        backup_database(target, backup_path)

        plan_id = uid("plan")
        series_id = uid("series")
        learning_run_id = uid("learning_run")
        book_map = {item["id"]: uid("book") for item in source_books}
        chapter_map = {item["id"]: uid("chapter") for item in source_chapters}
        section_map = {item["id"]: uid("section") for item in source_sections}
        content_map = {item["id"]: uid("content") for item in source_contents}

        assumptions = json.loads(source_series_row["assumptions_json"] or "[]")
        assumptions.append("本系列来自已中止的 M1 真实评测教材副本；未迁移评测 Agent 的答题、掌握度、笔记或成果证据。")
        details = (source_series_row["details"] or "").rstrip()
        details = f"{details}\n\n[{marker}] imported_at:{imported_at}".strip()

        target.execute("BEGIN IMMEDIATE")
        target.execute(
            "INSERT INTO learning_plans (id,shelf_id,topic,role,experience,purpose,depth,details,assumptions_json,confidence,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (plan_id, args.shelf_id, source_series_row["topic"], source_series_row["role"], source_series_row["experience"], source_series_row["purpose"], source_series_row["depth"], details, json.dumps(assumptions, ensure_ascii=False), source_series_row["confidence"], "active", imported_at),
        )
        target.execute(
            "INSERT INTO series (id,plan_id,shelf_id,title,rationale,deleted_at) VALUES (?,?,?,?,?,NULL)",
            (series_id, plan_id, args.shelf_id, source_series_row["title"], source_series_row["rationale"]),
        )
        target.execute(
            "INSERT INTO learning_runs (id,user_id,series_id,status,created_at,completed_at) VALUES (?,?,?,?,?,NULL)",
            (learning_run_id, args.user_id, series_id, "active", imported_at),
        )

        for book in source_books:
            new_book_id = book_map[book["id"]]
            target.execute(
                "INSERT INTO books (id,series_id,shelf_id,position,title,topic,description,estimated_minutes,deleted_at) VALUES (?,?,?,?,?,?,?,?,NULL)",
                (new_book_id, series_id, args.shelf_id, book["position"], book["title"], book["topic"], book["description"], book["estimated_minutes"]),
            )
            target.execute(
                "INSERT INTO book_progress (id,learning_run_id,user_id,book_id,status,updated_at) VALUES (?,?,?,?,?,?)",
                (uid("book_progress"), learning_run_id, args.user_id, new_book_id, "available" if book["position"] == 1 else "locked", imported_at),
            )

        book_positions = {item["id"]: item["position"] for item in source_books}
        chapter_positions = {item["id"]: item["position"] for item in source_chapters}
        for chapter in source_chapters:
            new_chapter_id = chapter_map[chapter["id"]]
            target.execute(
                "INSERT INTO chapters (id,book_id,position,title,objective) VALUES (?,?,?,?,?)",
                (new_chapter_id, book_map[chapter["book_id"]], chapter["position"], chapter["title"], chapter["objective"]),
            )
            is_first = book_positions[chapter["book_id"]] == 1 and chapter["position"] == 1
            target.execute(
                "INSERT INTO chapter_progress (id,learning_run_id,user_id,chapter_id,status,updated_at) VALUES (?,?,?,?,?,?)",
                (uid("chapter_progress"), learning_run_id, args.user_id, new_chapter_id, "available" if is_first else "locked", imported_at),
            )

        chapters_by_id = {item["id"]: item for item in source_chapters}
        for section in source_sections:
            new_section_id = section_map[section["id"]]
            target.execute(
                "INSERT INTO sections (id,chapter_id,position,title,question,objectives_json) VALUES (?,?,?,?,?,?)",
                (new_section_id, chapter_map[section["chapter_id"]], section["position"], section["title"], section["question"], section["objectives_json"]),
            )
            chapter = chapters_by_id[section["chapter_id"]]
            is_first = book_positions[chapter["book_id"]] == 1 and chapter_positions[chapter["id"]] == 1 and section["position"] == 1
            target.execute(
                "INSERT INTO section_progress (id,learning_run_id,user_id,section_id,status,best_score,total_score,ask_me_unlocked,version,updated_at) VALUES (?,?,?,?,?,0,0,0,1,?)",
                (uid("section_progress"), learning_run_id, args.user_id, new_section_id, "available" if is_first else "locked", imported_at),
            )

        for content in source_contents:
            target.execute(
                "INSERT INTO content_versions (id,section_id,version,blocks_json,sources_json,confidence,created_at) VALUES (?,?,?,?,?,?,?)",
                (content_map[content["id"]], section_map[content["section_id"]], content["version"], content["blocks_json"], content["sources_json"], content["confidence"], content["created_at"]),
            )
        for verification in source_verifications:
            target.execute(
                "INSERT INTO source_verifications (id,content_version_id,report_json,verified_at) VALUES (?,?,?,?)",
                (uid("verification"), content_map[verification["content_version_id"]], verification["report_json"], verification["verified_at"]),
            )
        for quiz in source_quizzes:
            target.execute(
                "INSERT INTO quiz_sets (id,section_id,content_version_id,generation,questions_json) VALUES (?,?,?,?,?)",
                (uid("quiz"), section_map[quiz["section_id"]], content_map[quiz["content_version_id"]], 1, quiz["questions_json"]),
            )
        for run in source_runs:
            trace = json.loads(run["trace_json"] or "{}")
            trace["importLineage"] = {"mode": "evaluation_catalog_copy", "snapshotSha256": source_sha, "sourceStatus": "interrupted", "learningFactsCopied": False, "importedAt": imported_at}
            target.execute(
                "INSERT INTO generation_runs (id,section_id,operation,attempt,status,model,trace_json,error_code,error_message,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (uid("generation"), section_map[run["section_id"]], "lesson", run["attempt"], "succeeded", run["model"], json.dumps(trace, ensure_ascii=False), "", "", run["started_at"], run["finished_at"]),
            )

        for capstone in source_capstones:
            new_id = uid("capstone")
            target.execute(
                "INSERT INTO book_capstones (id,book_id,title,brief_json,updated_at) VALUES (?,?,?,?,?)",
                (new_id, book_map[capstone["book_id"]], capstone["title"], capstone["brief_json"], imported_at),
            )
            target.execute(
                "INSERT INTO artifact_progress (id,learning_run_id,user_id,target_type,target_id,status,submission_json,updated_at) VALUES (?,?,?,?,?,'locked','{}',?)",
                (uid("artifact_progress"), learning_run_id, args.user_id, "book_capstone", new_id, imported_at),
            )
        for practice in source_practices:
            new_id = uid("practice")
            target.execute(
                "INSERT INTO chapter_practices (id,chapter_id,title,instructions_json,updated_at) VALUES (?,?,?,?,?)",
                (new_id, chapter_map[practice["chapter_id"]], practice["title"], practice["instructions_json"], imported_at),
            )
            target.execute(
                "INSERT INTO artifact_progress (id,learning_run_id,user_id,target_type,target_id,status,submission_json,updated_at) VALUES (?,?,?,?,?,'locked','{}',?)",
                (uid("artifact_progress"), learning_run_id, args.user_id, "chapter_practice", new_id, imported_at),
            )

        target.commit()
        print(json.dumps({"status": "imported", "seriesId": series_id, "backup": str(backup_path), **summary}, ensure_ascii=False))
    except Exception:
        if target.in_transaction:
            target.rollback()
        raise
    finally:
        source.close()
        target.close()


if __name__ == "__main__":
    main()
