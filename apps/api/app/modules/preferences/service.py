from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import re
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    ContentVersion,
    LearningPreferenceDecision,
    LearningPreferenceEvidence,
    PersonalBlockPresentation,
    QaMessage,
    QaSession,
    now,
)


HALF_LIFE_DAYS = 120.0
CONFIDENCE_PRIOR = 4.0
ACTIVATION_SCORE = 0.18
ACTIVATION_OUTCOMES = 3
ACTIVATION_CONTEXTS = 2

STYLE_DIMENSIONS = {
    "worked_example": {"example": 1.0},
    "diagram": {"diagram": 1.0},
    "analogy": {"analogy": 1.0},
    "derivation": {"derivation": 1.0},
    "precise": {"precision": 1.0},
    "concise": {"concise": 1.0},
}
SIGNAL_WEIGHTS = {
    "requested": 0.15,
    "helpful": 1.0,
    "unclear": -1.0,
    "adopted": 1.8,
}
DIMENSION_LABELS = {
    "example": "具体例子",
    "diagram": "图解关系",
    "analogy": "贴切类比",
    "derivation": "逐步推导",
    "precision": "严谨定义",
    "concise": "简洁要点",
    "plain_language": "通俗表达",
    "humor": "轻松幽默",
}
CUSTOM_PATTERNS = {
    "example": ("例子", "案例", "场景", "具体", "生活中", "生活里", "现实中"),
    "diagram": ("图表", "关系图", "流程图", "画图", "图解", "可视化"),
    "analogy": ("类比", "比方", "打比方", "像是", "好比"),
    "derivation": ("推导", "一步步", "每一步", "公式过程", "展开过程"),
    "precision": ("严谨", "准确定义", "成立条件", "适用边界", "准确一点"),
    "concise": ("简洁", "简短", "要点", "少一点", "别啰嗦", "不要啰嗦"),
    "plain_language": ("通俗", "白话", "少用术语", "不用术语", "零基础", "入门者"),
    "humor": ("幽默", "诙谐", "有趣", "轻松一点"),
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: str, default):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def extract_custom_dimensions(text: str) -> tuple[dict[str, float], float]:
    """Convert free text to bounded, auditable features without retaining the text."""
    normalized = re.sub(r"\s+", "", text.strip().lower())
    matched = {
        dimension: 1.0
        for dimension, patterns in CUSTOM_PATTERNS.items()
        if any(pattern in normalized for pattern in patterns)
    }
    if not matched:
        return {}, 0.0
    confidence = min(0.95, 0.7 + 0.08 * (len(matched) - 1))
    return matched, confidence


def require_published_block(
    db: Session,
    *,
    section_id: str,
    content_version_id: str,
    block_id: str,
) -> ContentVersion:
    content = db.get(ContentVersion, content_version_id)
    if (
        not content
        or content.section_id != section_id
        or content.publication_status != "published"
    ):
        raise AppError(
            "正文版本已经更新，请刷新后重试",
            code="PREFERENCE_CONTENT_VERSION_INVALID",
            status=409,
        )
    blocks = _load(content.blocks_json, [])
    if not any(
        isinstance(block, dict) and block.get("id") == block_id
        for block in blocks
    ):
        raise AppError(
            "当前正文中没有这个段落",
            code="PREFERENCE_BLOCK_NOT_FOUND",
            status=409,
        )
    return content


class LearningPreferenceService:
    def __init__(self, db: Session, user_id: str, clock=now):
        self.db = db
        self.user_id = user_id
        self.clock = clock

    def record(self, body, *, shelf_id: str) -> dict:
        return self._record(
            event_id=body.event_id,
            request_event_id=body.request_event_id,
            section_id=body.section_id,
            content_version_id=body.content_version_id,
            block_id=body.block_id,
            block_kind=body.block_kind,
            style=body.style,
            signal=body.signal,
            custom_instruction=body.custom_instruction,
            shelf_id=shelf_id,
        )

    def record_adoption(
        self,
        body,
        *,
        section_id: str,
        shelf_id: str,
        presentation: PersonalBlockPresentation,
    ) -> dict:
        """Record an adoption only after the server completed that action."""

        if (
            presentation.user_id != self.user_id
            or presentation.section_id != section_id
        ):
            raise AppError(
                "个人讲法与当前操作不一致",
                code="PREFERENCE_ADOPTION_INVALID",
                status=409,
            )
        if (
            presentation.content_version_id != body.content_version_id
            or presentation.block_id != body.block_id
            or presentation.source_qa_message_id != body.answer_message_id
            or not presentation.active
        ):
            raise AppError(
                "个人讲法与当前操作不一致",
                code="PREFERENCE_ADOPTION_INVALID",
                status=409,
            )
        return self._record(
            event_id=body.event_id,
            request_event_id=body.request_event_id,
            section_id=section_id,
            content_version_id=body.content_version_id,
            block_id=body.block_id,
            block_kind=body.block_kind,
            style=body.style,
            signal="adopted",
            custom_instruction=None,
            shelf_id=shelf_id,
            source_qa_message_id=body.answer_message_id,
        )

    def decide(self, body) -> dict:
        """Persist an explicit user decision before applying it to generation."""

        payload = body.model_dump(by_alias=True)
        request_hash = sha256(_json(payload).encode("utf-8")).hexdigest()
        existing = self.db.scalar(
            select(LearningPreferenceDecision).where(
                LearningPreferenceDecision.user_id == self.user_id,
                LearningPreferenceDecision.decision_key == body.decision_key,
            )
        )
        if existing:
            if existing.request_hash != request_hash:
                raise AppError(
                    "偏好决定编号已被用于不同内容",
                    code="PREFERENCE_DECISION_IDEMPOTENCY_CONFLICT",
                    status=409,
                )
            return self.projection(shelf_id=body.shelf_id, recorded=False)

        source = self.db.scalar(
            select(LearningPreferenceEvidence).where(
                LearningPreferenceEvidence.user_id == self.user_id,
                LearningPreferenceEvidence.event_id == body.request_event_id,
                LearningPreferenceEvidence.signal == "requested",
            )
        )
        if not source:
            raise AppError(
                "找不到对应的讲法请求",
                code="PREFERENCE_DECISION_SOURCE_NOT_FOUND",
                status=404,
            )
        dimensions = _load(source.dimensions_json, {})
        if body.dimension not in dimensions:
            raise AppError(
                "这次讲法没有提供该偏好依据",
                code="PREFERENCE_DECISION_DIMENSION_INVALID",
                status=409,
            )
        if body.scope_kind == "shelf" and source.shelf_id != body.shelf_id:
            raise AppError(
                "偏好决定与当前书架不一致",
                code="PREFERENCE_DECISION_SCOPE_INVALID",
                status=409,
            )
        occurred_at = self.clock()
        decision_sequence = (
            self.db.scalar(
                select(func.max(LearningPreferenceDecision.decision_sequence)).where(
                    LearningPreferenceDecision.user_id == self.user_id
                )
            )
            or 0
        ) + 1
        self.db.add(
            LearningPreferenceDecision(
                id=f"preference_decision_{uuid4().hex}",
                user_id=self.user_id,
                decision_key=body.decision_key,
                decision_sequence=decision_sequence,
                scope_kind=body.scope_kind,
                shelf_id=body.shelf_id if body.scope_kind == "shelf" else None,
                dimension=body.dimension,
                state=body.state,
                source_evidence_id=source.id,
                request_hash=request_hash,
                created_at=occurred_at,
            )
        )
        self.db.commit()
        return self.projection(shelf_id=body.shelf_id, recorded=True)

    def _record(
        self,
        *,
        event_id: str,
        request_event_id: str | None,
        section_id: str,
        content_version_id: str,
        block_id: str,
        block_kind: str,
        style: str,
        signal: str,
        custom_instruction: str | None,
        shelf_id: str,
        source_qa_message_id: str | None = None,
    ) -> dict:
        payload = {
            "eventId": event_id,
            "requestEventId": request_event_id,
            "sectionId": section_id,
            "contentVersionId": content_version_id,
            "blockId": block_id,
            "blockKind": block_kind,
            "style": style,
            "signal": signal,
            "customInstruction": custom_instruction,
            "sourceQaMessageId": source_qa_message_id,
        }
        request_hash = sha256(_json(payload).encode("utf-8")).hexdigest()
        existing = self.db.scalar(
            select(LearningPreferenceEvidence).where(
                LearningPreferenceEvidence.user_id == self.user_id,
                LearningPreferenceEvidence.event_id == event_id,
            )
        )
        if existing:
            if existing.request_hash != request_hash:
                raise AppError(
                    "偏好证据编号已被用于不同内容",
                    code="PREFERENCE_EVIDENCE_IDEMPOTENCY_CONFLICT",
                    status=409,
                )
            self.db.commit()
            return self.projection(shelf_id=shelf_id, recorded=False)

        require_published_block(
            self.db,
            section_id=section_id,
            content_version_id=content_version_id,
            block_id=block_id,
        )

        if signal == "requested":
            if request_event_id:
                raise AppError(
                    "首次讲法请求不能引用另一条证据",
                    code="PREFERENCE_EVIDENCE_INVALID",
                    status=400,
                )
            if style == "custom":
                dimensions, extraction_confidence = extract_custom_dimensions(
                    custom_instruction or ""
                )
                extractor_version = "bounded_zh_v1"
            else:
                dimensions = STYLE_DIMENSIONS[style]
                extraction_confidence = 1.0
                extractor_version = "preset_v1"
        else:
            if not request_event_id or custom_instruction:
                raise AppError(
                    "讲法反馈必须引用原请求，且不能重复携带自由文本",
                    code="PREFERENCE_EVIDENCE_INVALID",
                    status=400,
                )
            parent = self.db.scalar(
                select(LearningPreferenceEvidence).where(
                    LearningPreferenceEvidence.user_id == self.user_id,
                    LearningPreferenceEvidence.event_id == request_event_id,
                    LearningPreferenceEvidence.signal == "requested",
                )
            )
            if not parent or parent.section_id != section_id:
                raise AppError(
                    "找不到对应的讲法请求",
                    code="PREFERENCE_EVIDENCE_PARENT_NOT_FOUND",
                    status=404,
                )
            if parent.style != style:
                raise AppError(
                    "讲法反馈与原请求不一致",
                    code="PREFERENCE_EVIDENCE_INVALID",
                    status=400,
                )
            if (
                parent.content_version_id != content_version_id
                or parent.block_id != block_id
                or parent.block_kind != block_kind
            ):
                raise AppError(
                    "讲法反馈与原请求的正文段落不一致",
                    code="PREFERENCE_EVIDENCE_INVALID",
                    status=400,
                )
            terminal = self.db.scalar(
                select(LearningPreferenceEvidence)
                .where(
                    LearningPreferenceEvidence.user_id == self.user_id,
                    LearningPreferenceEvidence.terminal_request_key
                    == request_event_id,
                )
            )
            if terminal:
                if terminal.signal == signal:
                    self.db.commit()
                    return self.projection(shelf_id=shelf_id, recorded=False)
                raise AppError(
                    "这次讲法已经反馈过了",
                    code="PREFERENCE_FEEDBACK_ALREADY_RECORDED",
                    status=409,
                )
            dimensions = _load(parent.dimensions_json, {})
            extraction_confidence = parent.extraction_confidence
            extractor_version = parent.extractor_version

        occurred_at = self.clock()
        self.db.add(
            LearningPreferenceEvidence(
                id=f"preference_evidence_{uuid4().hex}",
                user_id=self.user_id,
                event_id=event_id,
                request_event_id=request_event_id or "",
                terminal_request_key=(
                    request_event_id
                    if signal in {"helpful", "unclear", "adopted"}
                    else None
                ),
                section_id=section_id,
                shelf_id=shelf_id,
                content_version_id=content_version_id or "",
                block_id=block_id,
                block_kind=block_kind,
                style=style,
                signal=signal,
                dimensions_json=_json(dimensions),
                extraction_confidence=extraction_confidence,
                extractor_version=extractor_version,
                request_hash=request_hash,
                occurred_at=occurred_at,
                created_at=occurred_at,
            )
        )
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            if signal not in {"helpful", "unclear", "adopted"}:
                raise
            terminal = self.db.scalar(
                select(LearningPreferenceEvidence).where(
                    LearningPreferenceEvidence.user_id == self.user_id,
                    LearningPreferenceEvidence.terminal_request_key
                    == request_event_id,
                )
            )
            if terminal and terminal.signal == signal:
                return self.projection(shelf_id=shelf_id, recorded=False)
            raise AppError(
                "这次讲法已经反馈过了",
                code="PREFERENCE_FEEDBACK_ALREADY_RECORDED",
                status=409,
            )
        return self.projection(shelf_id=shelf_id, recorded=True)

    def projection(self, *, shelf_id: str | None = None, recorded: bool | None = None) -> dict:
        rows = self._effective_evidence(list(
            self.db.scalars(
                select(LearningPreferenceEvidence).where(
                    LearningPreferenceEvidence.user_id == self.user_id
                )
            )
        ))
        global_stats = self._stats(rows)
        domain_rows = [row for row in rows if shelf_id and row.shelf_id == shelf_id]
        domain_stats = self._stats(domain_rows) if shelf_id else {}
        keys = sorted(set(global_stats) | set(domain_stats))
        dimensions = []
        for key in keys:
            global_item = global_stats.get(key, self._empty_stat(key))
            domain_item = domain_stats.get(key)
            if domain_item:
                domain_weight = domain_item["effectiveEvidence"] / (
                    domain_item["effectiveEvidence"] + CONFIDENCE_PRIOR
                )
                score = domain_weight * domain_item["score"] + (1 - domain_weight) * global_item["score"]
                confidence = max(global_item["confidence"] * 0.6, domain_item["confidence"])
                positive_outcomes = domain_item["positiveOutcomes"]
                context_count = domain_item["contextCount"]
            else:
                score = global_item["score"]
                confidence = global_item["confidence"]
                positive_outcomes = global_item["positiveOutcomes"]
                context_count = global_item["contextCount"]
            active = (
                score >= ACTIVATION_SCORE
                and positive_outcomes >= ACTIVATION_OUTCOMES
                and context_count >= ACTIVATION_CONTEXTS
            )
            dimensions.append({
                "key": key,
                "label": DIMENSION_LABELS.get(key, key),
                "score": round(score, 4),
                "confidence": round(confidence, 4),
                "evidenceCount": global_item["evidenceCount"],
                "positiveOutcomes": positive_outcomes,
                "contextCount": context_count,
                "active": active,
            })
        suggested = self._effective_preferences(dimensions)
        confirmed = self._confirmed_preferences(shelf_id=shelf_id)
        result = {
            "updatedAt": self.clock().isoformat(),
            "dimensions": dimensions,
            "suggestedPreferences": suggested,
            "confirmedPreferences": confirmed,
            # Compatibility field: only explicit authority is effective.
            "effectivePreferences": confirmed,
        }
        if recorded is not None:
            result["recorded"] = recorded
        return result

    @staticmethod
    def _effective_evidence(
        rows: list[LearningPreferenceEvidence],
    ) -> list[LearningPreferenceEvidence]:
        """Project at most one terminal outcome for each explanation request."""

        ordinary: list[LearningPreferenceEvidence] = []
        terminal_by_request: dict[str, LearningPreferenceEvidence] = {}
        for row in rows:
            if (
                row.request_event_id
                and row.signal in {"helpful", "unclear", "adopted"}
            ):
                current = terminal_by_request.get(row.request_event_id)
                if current is None or (row.occurred_at, row.id) > (
                    current.occurred_at,
                    current.id,
                ):
                    terminal_by_request[row.request_event_id] = row
            else:
                ordinary.append(row)
        return [*ordinary, *terminal_by_request.values()]

    def effective_preferences(self, explicit: dict, *, shelf_id: str | None = None) -> dict:
        confirmed = self._confirmed_preferences(shelf_id=shelf_id)
        result = dict(explicit)
        if result.get("openingStyle", "auto") == "auto" and confirmed.get("openingStyle"):
            result["openingStyle"] = confirmed["openingStyle"]
        if result.get("explanationDensity", "auto") == "auto" and confirmed.get("explanationDensity"):
            result["explanationDensity"] = confirmed["explanationDensity"]
        explicit_formats = list(result.get("formatPreferences") or [])
        result["formatPreferences"] = list(dict.fromkeys(explicit_formats + confirmed.get("formatPreferences", [])))[:5]
        if confirmed.get("styleGuidance"):
            result["styleGuidance"] = confirmed["styleGuidance"]
        return result

    def _confirmed_preferences(self, *, shelf_id: str | None) -> dict:
        rows = list(
            self.db.scalars(
                select(LearningPreferenceDecision)
                .where(LearningPreferenceDecision.user_id == self.user_id)
                .order_by(
                    LearningPreferenceDecision.decision_sequence,
                )
            )
        )
        latest: dict[tuple[str, str | None, str], LearningPreferenceDecision] = {}
        for row in rows:
            key = (row.scope_kind, row.shelf_id, row.dimension)
            latest[key] = row
        active: set[str] = {
            dimension
            for (scope_kind, scope_shelf_id, dimension), row in latest.items()
            if scope_kind == "global" and scope_shelf_id is None and row.state == "confirmed"
        }
        if shelf_id:
            for (scope_kind, scope_shelf_id, dimension), row in latest.items():
                if scope_kind != "shelf" or scope_shelf_id != shelf_id:
                    continue
                if row.state == "confirmed":
                    active.add(dimension)
                else:
                    active.discard(dimension)
        return self._effective_preferences([
            {"key": key, "active": True, "score": 1.0}
            for key in sorted(active)
        ])

    def _stats(self, rows: list[LearningPreferenceEvidence]) -> dict[str, dict]:
        current = _aware(self.clock())
        accumulators: dict[str, dict] = {}
        for row in rows:
            dimensions = _load(row.dimensions_json, {})
            age_days = max(0.0, (current - _aware(row.occurred_at)).total_seconds() / 86400)
            decay = math.exp(-math.log(2) * age_days / HALF_LIFE_DAYS)
            signal_weight = SIGNAL_WEIGHTS.get(row.signal, 0.0)
            for key, dimension_weight in dimensions.items():
                contribution = signal_weight * float(dimension_weight) * row.extraction_confidence * decay
                item = accumulators.setdefault(key, {
                    "positive": 0.0,
                    "negative": 0.0,
                    "evidenceCount": 0,
                    "positiveOutcomes": 0,
                    "contexts": set(),
                })
                item["positive"] += max(contribution, 0.0)
                item["negative"] += max(-contribution, 0.0)
                item["evidenceCount"] += 1
                if row.signal in {"helpful", "adopted"}:
                    item["positiveOutcomes"] += 2 if row.signal == "adopted" else 1
                    item["contexts"].add(row.section_id)
        result = {}
        for key, item in accumulators.items():
            alpha = 1.0 + item["positive"]
            beta = 1.0 + item["negative"]
            effective = item["positive"] + item["negative"]
            mean = alpha / (alpha + beta)
            confidence = effective / (effective + CONFIDENCE_PRIOR)
            result[key] = {
                "score": (2 * mean - 1) * confidence,
                "confidence": confidence,
                "effectiveEvidence": effective,
                "evidenceCount": item["evidenceCount"],
                "positiveOutcomes": item["positiveOutcomes"],
                "contextCount": len(item["contexts"]),
            }
        return result

    @staticmethod
    def _empty_stat(key: str) -> dict:
        return {
            "score": 0.0,
            "confidence": 0.0,
            "effectiveEvidence": 0.0,
            "evidenceCount": 0,
            "positiveOutcomes": 0,
            "contextCount": 0,
        }

    @staticmethod
    def _effective_preferences(dimensions: list[dict]) -> dict:
        active = {item["key"]: item for item in dimensions if item["active"]}
        formats = [
            mapped
            for key, mapped in (
                ("example", "worked_example"),
                ("diagram", "diagram"),
                ("analogy", "analogy"),
            )
            if key in active
        ]
        density = None
        if "concise" in active and "derivation" in active:
            density = "concise" if active["concise"]["score"] >= active["derivation"]["score"] else "thorough"
        elif "concise" in active:
            density = "concise"
        elif "derivation" in active:
            density = "thorough"
        guidance = [
            label
            for key, label in (("plain_language", "prefer_plain_language"), ("humor", "prefer_light_humor"))
            if key in active
        ]
        return {
            "openingStyle": "concept_first" if "precision" in active else None,
            "explanationDensity": density,
            "formatPreferences": formats,
            "styleGuidance": guidance,
        }


class PersonalPresentationService:
    def __init__(self, db: Session, user_id: str):
        self.db = db
        self.user_id = user_id

    def adopt(self, body, *, section_id: str) -> PersonalBlockPresentation:
        require_published_block(
            self.db,
            section_id=section_id,
            content_version_id=body.content_version_id,
            block_id=body.block_id,
        )
        session = self.db.scalar(
            select(QaSession).where(
                QaSession.user_id == self.user_id,
                QaSession.section_id == section_id,
                QaSession.content_version_id == body.content_version_id,
            )
        )
        if not session:
            raise AppError("找不到这次答疑", code="QA_SESSION_NOT_FOUND", status=404)
        message = self.db.scalar(
            select(QaMessage)
            .where(
                QaMessage.id == body.answer_message_id,
                QaMessage.session_id == session.id,
                QaMessage.thread_id == body.thread_id,
                QaMessage.block_id == body.block_id,
                QaMessage.role == "assistant",
                QaMessage.preference_request_event_id == body.request_event_id,
                QaMessage.explanation_style == body.style,
                QaMessage.explanation_block_kind == body.block_kind,
                QaMessage.request_source == "explanation_preference",
            )
        )
        if not message:
            raise AppError("找不到可保留的讲法", code="QA_ANSWER_NOT_FOUND", status=404)
        override = self.db.scalar(
            select(PersonalBlockPresentation).where(
                PersonalBlockPresentation.user_id == self.user_id,
                PersonalBlockPresentation.content_version_id == body.content_version_id,
                PersonalBlockPresentation.block_id == body.block_id,
            )
        )
        timestamp = now()
        if not override:
            override = PersonalBlockPresentation(
                id=f"personal_presentation_{uuid4().hex}",
                user_id=self.user_id,
                section_id=section_id,
                content_version_id=body.content_version_id,
                block_id=body.block_id,
                replacement_content=message.content,
                source_qa_message_id=message.id,
                active=True,
                created_at=timestamp,
                updated_at=timestamp,
            )
            self.db.add(override)
        else:
            override.replacement_content = message.content
            override.source_qa_message_id = message.id
            override.active = True
            override.updated_at = timestamp
        self.db.flush()
        return override

    def restore(self, *, content_version_id: str, block_id: str) -> None:
        override = self.db.scalar(
            select(PersonalBlockPresentation).where(
                PersonalBlockPresentation.user_id == self.user_id,
                PersonalBlockPresentation.content_version_id == content_version_id,
                PersonalBlockPresentation.block_id == block_id,
            )
        )
        if not override:
            raise AppError("没有需要恢复的个人讲法", code="PERSONAL_PRESENTATION_NOT_FOUND", status=404)
        override.active = False
        override.updated_at = now()
        self.db.commit()
