import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.infrastructure.tables import (
    AssessmentTarget, Base, Book, Chapter, Concept, ConceptRevision,
    KnowledgeNodeStateProjection, KnowledgeStateProjection,
    LearningContractAssessmentTarget, LearningContractVersion,
    LearningMissionVersion, LearningPlan, Section, Series, Shelf, User, now,
)
from app.modules.learning.knowledge_map import KnowledgeMapService


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed_route(db: Session) -> None:
    moment = now()
    db.add_all([
        User(id="user_map", name="学习者"),
        Shelf(id="shelf_map", user_id="user_map", name="数学", domain="数学", specialty="微积分"),
        LearningPlan(id="plan_map", shelf_id="shelf_map", topic="导数", role="学习者", experience="基础代数", depth="deep", confidence="high"),
        LearningMissionVersion(id="mission_map", plan_id="plan_map", user_id="user_map", version=1, status="confirmed", why="解决最优化问题", payload_hash="a" * 64, confirmed_at=moment),
        Series(id="series_map", plan_id="plan_map", shelf_id="shelf_map", title="导数能力路线", rationale="从变化率到应用", initial_mission_version_id="mission_map"),
        Book(id="book_map", series_id="series_map", shelf_id="shelf_map", position=1, title="导数应用", topic="导数", description="导数应用", estimated_minutes=180),
        Chapter(id="chapter_map", book_id="book_map", position=1, title="变化率", objective="解释变化率"),
        Section(id="section_map", chapter_id="chapter_map", position=1, title="瞬时变化率", question="导数表示什么？", objectives_json='["解释瞬时变化率"]'),
        Concept(id="concept_map", namespace="test", concept_key="instant_rate", canonical_name="瞬时变化率", origin="test"),
        ConceptRevision(id="revision_map", concept_id="concept_map", revision=1, label="瞬时变化率", definition="解释导数表示的瞬时变化率。", scope_json=json.dumps({"rankPolicy": {"version": "knowledge_rank_policy_v1", "capabilityScope": "解释导数表示的瞬时变化率", "rankCeiling": "silver", "dimensionRanks": {"recognition": "bronze", "mechanism": "silver"}}}), verification_status="reviewed"),
        AssessmentTarget(id="target_map", concept_revision_id="revision_map", objective_key="instant_rate", objective_statement="解释导数表示的瞬时变化率", dimension="mechanism", target_depth="deep", identity_status="published_knowledge_graph", status="active"),
        LearningContractVersion(id="contract_map", section_id="section_map", mission_version_id="mission_map", version=1, section_question_snapshot="导数表示什么？", target_depth="deep", contract_hash="b" * 64),
    ])
    db.flush()
    db.add_all([
        LearningContractAssessmentTarget(id="binding_map", contract_version_id="contract_map", assessment_target_id="target_map", position=1, required=True, diagnostic_only=False),
        KnowledgeStateProjection(id="state_map", user_id="user_map", assessment_target_id="target_map", p_known_ppm=800000, uncertainty_ppm=300000, claim_status="verified_immediate", parameter_set_version="bkt_multimodal_v2", projection_rule_version="mastery_v3", source_observation_watermark=3),
        KnowledgeNodeStateProjection(id="node_map", user_id="user_map", concept_revision_id="revision_map", current_rank="silver", current_rank_order=2, current_stars=2, highest_rank="silver", highest_rank_order=2, highest_stars=2, activation_state="active", stability_days=3, evidence_count=2, independent_evidence_count=2, uncertainty_ppm=300000, rank_rule_version="knowledge_rank_v2", source_observation_watermark=3),
    ])
    db.commit()


def test_personal_map_uses_latest_contract_and_qualified_projection() -> None:
    with _session() as db:
        _seed_route(db)
        result = KnowledgeMapService(db, user_id="user_map").view(series_id="series_map")
        assert result["availability"] == "ready"
        assert result["progress"]["coveragePpm"] == 1_000_000
        assert result["nodes"][0]["rankLabel"] == "白银 · 理解"
        assert result["nodes"][0]["capabilityScope"] == "解释导数表示的瞬时变化率"
        assert result["nodes"][0]["nextAction"]["kind"] == "maintain"
        assert result["learnerProfile"]["rankedNodeCount"] == 1


def test_personal_map_rejects_another_users_series() -> None:
    with _session() as db:
        _seed_route(db)
        with pytest.raises(AppError) as error:
            KnowledgeMapService(db, user_id="other_user").view(series_id="series_map")
        assert error.value.code == "SERIES_NOT_FOUND"
