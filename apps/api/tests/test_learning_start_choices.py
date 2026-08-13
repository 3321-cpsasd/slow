from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.schemas import LearningStartPreviewCreate, PlanCreate
from app.core.errors import AppError
from app.infrastructure.tables import (
    Base,
    Concept,
    ConceptRelationVersion,
    ConceptRevision,
    KnowledgeGraphRelease,
    SeriesLearningStartPreference,
)
from app.modules.learning.learning_start import LearningStartService


class PublishedBaselineStub:
    def __init__(self):
        self.baseline = SimpleNamespace(
            id="baseline_learning_start",
            title="算法问题求解",
        )

    def select_for_plan(self, **_kwargs):
        return self.baseline


def _service(db: Session) -> LearningStartService:
    return LearningStartService(
        db,
        user_id="user_learning_start",
        baselines=PublishedBaselineStub(),
        shelf_provider=lambda _shelf_id: SimpleNamespace(
            id="shelf_learning_start",
            domain="计算机",
            specialty="算法",
        ),
    )


def _published_graph(db: Session) -> None:
    revisions = []
    for key, label in (
        ("recursion", "递归"),
        ("search", "搜索"),
        ("dynamic", "动态规划"),
    ):
        concept = Concept(
            id=f"concept_{key}",
            namespace="learning_start_test",
            concept_key=key,
            canonical_name=label,
        )
        revision = ConceptRevision(
            id=f"revision_{key}",
            concept_id=concept.id,
            revision=1,
            label=label,
            definition=f"{label}的测试定义",
            verification_status="reviewed",
        )
        db.add_all([concept, revision])
        revisions.append(revision)
    release = KnowledgeGraphRelease(
        id="release_learning_start",
        baseline_version_id="baseline_learning_start",
        version=1,
        status="published",
        manifest_json=(
            '{"conceptRevisionIds":['
            + ",".join(f'"{item.id}"' for item in revisions)
            + "]}"
        ),
        content_hash="learning_start_release_hash",
    )
    db.add(release)
    db.add_all([
        ConceptRelationVersion(
            id="relation_recursion_search",
            release_id=release.id,
            from_concept_revision_id="revision_recursion",
            to_concept_revision_id="revision_search",
            relation_type="prerequisite_for",
            relation_revision=1,
            status="published",
        ),
        ConceptRelationVersion(
            id="relation_search_dynamic",
            release_id=release.id,
            from_concept_revision_id="revision_search",
            to_concept_revision_id="revision_dynamic",
            relation_type="contrasts_with",
            relation_revision=1,
            status="published",
        ),
    ])
    db.commit()


def test_guided_start_uses_only_visible_reviewed_graph_nodes():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _published_graph(db)
        service = _service(db)
        preview_body = LearningStartPreviewCreate.model_validate({
            "shelfId": "shelf_learning_start",
            "topic": "算法问题求解",
            "role": "工程师",
            "experience": "写过基础程序",
            "purpose": "提高问题建模能力",
            "depth": "deep",
            "details": "",
        })

        preview = service.preview(preview_body)
        assert preview["availability"] == "ready"
        assert {item["label"] for item in preview["nodes"]} == {
            "递归",
            "搜索",
            "动态规划",
        }
        assert len(preview["edges"]) == 2

        plan = PlanCreate.model_validate({
            **preview_body.model_dump(by_alias=True),
            "startMode": "guided",
            "learningStartSelection": {
                "previewId": preview["previewId"],
                "selectedConceptRevisionIds": ["revision_search"],
                "learningPreferences": ["practical_application"],
            },
        })
        context = service.planning_context(plan)
        assert [item["label"] for item in context["selectedKnowledge"]] == ["搜索"]
        assert {item["label"] for item in context["deprioritizedKnowledge"]} == {
            "递归",
            "动态规划",
        }
        service.bind_series(series_id="series_learning_start", body=plan)
        db.commit()
        stored = db.scalar(select(SeriesLearningStartPreference))
        assert stored.start_mode == "guided"
        assert stored.learning_preferences_json == '["practical_application"]'

        forged = PlanCreate.model_validate({
            **preview_body.model_dump(by_alias=True),
            "startMode": "guided",
            "learningStartSelection": {
                "previewId": preview["previewId"],
                "selectedConceptRevisionIds": ["revision_not_visible"],
            },
        })
        with pytest.raises(AppError) as error:
            service.planning_context(forged)
        assert error.value.code == "LEARNING_START_SELECTION_OUT_OF_SCOPE"
